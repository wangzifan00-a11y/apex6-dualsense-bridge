using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace DSBridge.HotSwitch {
    // This record is produced by the privileged service, never by pipe callers.
    public sealed class RecoveryRecord {
        public int schema;
        public string phase, device_id, root_id, root_signature;
        public Dictionary<string, string> branch;
        public uint status_before, problem_before;
        public bool pre_needs_restart;
        public UsbPortRecord usb;
    }
    internal interface IRecoveryStore {
        RecoveryRecord Load();
        void Save(RecoveryRecord record);
    }
    internal sealed class DurableJournal : IRecoveryStore {
        readonly string directory, path, temporary;
        internal DurableJournal() {
            directory = Installer.StateDirectory(false);
            path = Path.Combine(directory, "recovery.json");
            temporary = Path.Combine(directory, "recovery.next.json");
        }
        static JavaScriptSerializer Json() { return new JavaScriptSerializer {MaxJsonLength = 32768, RecursionLimit = 8}; }
        internal static RecoveryRecord Parse(string text) {
            var fields = Json().DeserializeObject(text) as Dictionary<string, object>;
            if (fields == null || (fields.Count != 6 && fields.Count != 9 && fields.Count != 10)) throw new Rejected("invalid_journal", "The recovery record is invalid.");
            foreach (string key in fields.Keys)
                if (key != "schema" && key != "phase" && key != "device_id" && key != "root_id" && key != "root_signature" && key != "branch" &&
                    key != "status_before" && key != "problem_before" && key != "pre_needs_restart" && key != "usb")
                    throw new Rejected("invalid_journal", "Unknown recovery field.");
            object usb;
            if (fields.TryGetValue("usb", out usb) && usb != null) UsbPortPolicy.ValidateFields(usb);
            var record = Json().Deserialize<RecoveryRecord>(text);
            // 1.0.2 journals predate diagnostic pre-state fields. Their verified
            // prepare path required started/problem-zero and no restart bit.
            if (fields.Count == 6 && record != null) record.status_before = 8;
            if (record != null && record.branch != null) {
                try { record.branch = new Dictionary<string, string>(record.branch, StringComparer.OrdinalIgnoreCase); }
                catch (ArgumentException) { throw new Rejected("invalid_journal", "Recovery identities must be unique."); }
            }
            Identity.ValidateRecord(record); return record;
        }
        internal static RecoveryRecord Copy(RecoveryRecord record) { return Parse(Json().Serialize(record)); }
        public RecoveryRecord Load() {
            Installer.Protected(directory, true);
            if (!File.Exists(path)) return null;
            Installer.Protected(path, false);
            var info = new FileInfo(path);
            if (info.Length < 2 || info.Length > 32768) throw new Rejected("invalid_journal", "The recovery record has an invalid size.");
            return Parse(File.ReadAllText(path, new UTF8Encoding(false, true)));
        }
        public void Save(RecoveryRecord record) {
            Identity.ValidateRecord(record);
            Installer.Protected(directory, true);
            if (File.Exists(path)) Installer.Protected(path, false);
            if (File.Exists(temporary)) Installer.Protected(temporary, false);
            byte[] bytes = new UTF8Encoding(false).GetBytes(Json().Serialize(record));
            // Both paths are fixed beneath an administrator-owned directory.
            // Flush before atomic replacement: no disable is allowed before this returns.
            using (var output = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None, 4096, FileOptions.WriteThrough)) {
                output.Write(bytes, 0, bytes.Length); output.Flush(true);
            }
            Installer.Protected(temporary, false);
            if (File.Exists(path)) File.Replace(temporary, path, null);
            else File.Move(temporary, path);
            Installer.Protected(path, false);
        }
    }
    internal static class Identity {
        static string Hash(string text) {
            using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(text))).Replace("-", "").ToLowerInvariant();
        }
        internal static string Fingerprint(Node node) {
            var ids = new List<string>();
            foreach (string value in node.Compatible) ids.Add(value.ToUpperInvariant());
            ids.Sort(StringComparer.Ordinal);
            return Hash((node.Id + "\n" + node.Service + "\n" + node.ClassGuid).ToUpperInvariant() + "\n" + String.Join("\n", ids));
        }
        internal static string Ancestors(Node root) {
            var values = new List<string>(); var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            for (Node node = root; node != null; node = node.Parent) {
                if (!seen.Add(node.Id) || seen.Count > 32) throw new Rejected("invalid_tree", "Recovery ancestry is invalid.");
                values.Add(Fingerprint(node));
            }
            return Hash(String.Join("\n", values));
        }
        static void Snapshot(Node node, Dictionary<string, string> result) {
            if (result.Count >= 4 || result.ContainsKey(node.Id)) throw new Rejected("invalid_tree", "Recovery branch is not exclusive.");
            result.Add(node.Id, Fingerprint(node));
            foreach (Node child in node.Children) Snapshot(child, result);
        }
        internal static RecoveryRecord Capture(Node leaf) {
            Node root = Policy.Select(leaf);
            var record = new RecoveryRecord {schema = 1, phase = "armed", device_id = leaf.Id, root_id = root.Id,
                root_signature = Ancestors(root), branch = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase), status_before = 8};
            Snapshot(root, record.branch); ValidateRecord(record); return record;
        }
        internal static void ValidateRecord(RecoveryRecord record) {
            if (record == null || (record.schema != 1 && record.schema != 2) || (record.phase != "armed" && record.phase != "completed") ||
                String.IsNullOrEmpty(record.device_id) || !Protocol.Id.IsMatch(record.device_id) || String.IsNullOrEmpty(record.root_id) ||
                !Regex.IsMatch(record.root_id, @"^(USB\\VID_37D7&PID_2502&MI_00|BTHENUM\\\{00001124-0000-1000-8000-00805F9B34FB\}_VID&[0-9A-F]{8}_PID&[0-9A-F]{4})\\[^\\\x00-\x20\x7f]{1,240}$", RegexOptions.IgnoreCase) ||
                record.branch == null || record.branch.Count < 2 || record.branch.Count > 3 || !record.branch.ContainsKey(record.root_id) ||
                !record.branch.ContainsKey(record.device_id) || !Regex.IsMatch(record.root_signature ?? "", "^[0-9a-f]{64}$") ||
                (record.status_before & 8) == 0 || (record.status_before & 0x400) != 0 || record.pre_needs_restart != ((record.status_before & 0x100) != 0))
                throw new Rejected("invalid_journal", "The recovery record cannot authorize a device operation.");
            foreach (var pair in record.branch)
                if (pair.Key.Length > 512 || !Regex.IsMatch(pair.Value ?? "", "^[0-9a-f]{64}$"))
                    throw new Rejected("invalid_journal", "Invalid recovery fingerprint.");
            if (record.schema == 1 && record.usb != null) throw new Rejected("invalid_journal", "Legacy records cannot contain USB operations.");
            if (record.schema == 2) UsbPortPolicy.ValidateRecord(record);
        }
        internal static void Verify(RecoveryRecord record, Node root) {
            ValidateRecord(record);
            if (root == null || !String.Equals(record.root_id, root.Id, StringComparison.OrdinalIgnoreCase) || Ancestors(root) != record.root_signature)
                throw new Rejected("recovery_identity_changed", "The original physical input identity changed; recovery remains pending.");
            var current = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase); Snapshot(root, current);
            foreach (var pair in current) {
                string expected;
                if (!record.branch.TryGetValue(pair.Key, out expected) || expected != pair.Value)
                    throw new Rejected("recovery_identity_changed", "The original input branch contains an unexpected device.");
            }
            // Missing children are expected while the previously validated root is
            // disabled. Recovery only enables that exact root, never a new target.
        }
        internal static void VerifyUsbRoot(RecoveryRecord record, Node root) {
            ValidateRecord(record);
            string expected;
            if (root == null || !record.root_id.StartsWith("USB\\VID_37D7&PID_2502&MI_00\\", StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(record.root_id, root.Id, StringComparison.OrdinalIgnoreCase) || Ancestors(root) != record.root_signature ||
                !record.branch.TryGetValue(record.root_id, out expected) || expected != Fingerprint(root))
                throw new Rejected("recovery_identity_changed", "The original physical input root or its ancestry changed.");
            // USB cycle can recreate IG/HID descendants with a different IG
            // index. They must subsequently pass Policy.Select as one complete,
            // exclusive game-input chain anchored to this unchanged MI_00.
        }
    }
    internal sealed class DeviceObservation {
        internal uint Status, Problem;
        internal bool HasProblem { get { return (Status & 0x400) != 0; } }
        internal bool NeedsRestart { get { return (Status & 0x100) != 0 || (HasProblem && Problem == 0x0e); } }
        internal bool CanBegin { get { return (Status & 8) != 0 && !HasProblem; } }
        internal bool Healthy { get { return CanBegin && !NeedsRestart; } }
    }
    internal interface IDeviceOperations {
        RecoveryRecord Prepare(string device);
        void Verify(RecoveryRecord record);
        uint Disable(RecoveryRecord record);
        uint Enable(RecoveryRecord record);
        DeviceObservation Observe(RecoveryRecord record);
    }
    internal sealed class SwitchTransaction {
        readonly IRecoveryStore store;
        readonly IDeviceOperations devices;
        readonly IUsbPortOperations usb;
        internal string LastRecoveryError;
        internal bool LastRecoveryRestartRequired;
        internal SwitchTransaction(IRecoveryStore journal, IDeviceOperations api, IUsbPortOperations portApi = null) { store = journal; devices = api; usb = portApi; }
        internal bool Pending() { RecoveryRecord record = store.Load(); return record != null && record.phase == "armed"; }
        internal Dictionary<string, object> RecoverPending() {
            RecoveryRecord record = store.Load();
            if (record == null || record.phase == "completed") { LastRecoveryError = null; LastRecoveryRestartRequired = false; return null; }
            return record.schema == 2 ? RecoverUsb(record, false) : Recover(record, false);
        }
        Dictionary<string, object> Recover(RecoveryRecord record, bool forceEnable) {
            var reply = Protocol.Reply(false, "recovery_pending", "The original input still needs enable recovery.");
            reply["root_id"] = record.root_id; reply["method"] = "cm_disable_enable";
            reply["status_before"] = record.status_before; reply["problem_before"] = record.problem_before;
            reply["pre_needs_restart"] = record.pre_needs_restart;
            reply["restart_required"] = LastRecoveryRestartRequired;
            try {
                devices.Verify(record);
                DeviceObservation state = forceEnable ? null : devices.Observe(record);
                uint enabled = 0;
                bool attemptEnable = forceEnable || !state.CanBegin;
                if (attemptEnable) {
                    enabled = devices.Enable(record); reply["enable_result"] = enabled;
                    if (enabled == 0x22) LastRecoveryRestartRequired = true;
                    reply["restart_required"] = LastRecoveryRestartRequired;
                    state = devices.Observe(record);
                }
                reply["enable_attempted"] = attemptEnable;
                reply["status"] = state.Status; reply["problem"] = state.Problem;
                LastRecoveryRestartRequired = enabled == 0x22 || state.NeedsRestart;
                reply["restart_required"] = LastRecoveryRestartRequired;
                devices.Verify(record);
                if (enabled != 0 || !state.Healthy) {
                    LastRecoveryError = state.CanBegin && LastRecoveryRestartRequired
                        ? "The original input is enabled, but Windows still requires a system restart."
                        : "Windows has not confirmed that the original input is enabled.";
                    reply["message"] = LastRecoveryError; return reply;
                }
                RecoveryRecord completed = DurableJournal.Copy(record); completed.phase = "completed"; store.Save(completed);
                LastRecoveryError = null; LastRecoveryRestartRequired = false;
                reply["ok"] = true; reply["code"] = "enabled"; reply["message"] = "The original physical input is enabled.";
                return reply;
            } catch (Exception error) {
                LastRecoveryError = error.Message; reply["message"] = LastRecoveryError;
                reply["recovery_error"] = error is Rejected ? ((Rejected)error).Code : "recovery_failed";
                return reply;
            }
        }
        internal Dictionary<string, object> Restart(string device) {
            RecoveryRecord prior = store.Load();
            Dictionary<string, object> pending = RecoverPending();
            if (pending != null && !Object.Equals(pending["ok"], true)) return pending;
            RecoveryRecord record, alias = null;
            try { record = devices.Prepare(device); }
            catch (Rejected missing) {
                // This is a specific alias proven by the last protected USB
                // transaction, including after background recovery completed.
                // There is no VID/PID search or mapping of unrelated stale IDs.
                if (missing.Code != "device_absent" || usb == null || prior == null || prior.schema != 2 ||
                    !String.Equals(prior.device_id, device, StringComparison.OrdinalIgnoreCase)) throw;
                usb.Verify(prior);
                Node current = usb.LoadInput(prior);
                Node root = Policy.Select(current); Identity.VerifyUsbRoot(prior, root);
                record = devices.Prepare(current.Id);
                if (!String.Equals(record.root_id, prior.root_id, StringComparison.OrdinalIgnoreCase) || record.root_signature != prior.root_signature)
                    throw new Rejected("usb_identity_changed", "The recovered request alias no longer belongs to the original controller.");
                alias = prior;
            }
            if (usb != null && record.root_id.StartsWith("USB\\", StringComparison.OrdinalIgnoreCase)) {
                record.usb = usb.Prepare(record); record.schema = 2;
                if (alias != null) UsbPortPolicy.Match(alias.usb, record.usb);
                Identity.ValidateRecord(record);
                return RestartUsb(record);
            }
            store.Save(record); // Durable intent precedes even the first device API.
            uint disabled = UInt32.MaxValue;
            string failure = null, failureCode = "disable_failed";
            Dictionary<string, object> restored = null;
            try {
                devices.Verify(record);
                // SDK ABSOLUTE + UI_NOT_OK; never PERSIST or HARDWARE. This does
                // not promise every Windows driver supports immediate switching.
                disabled = devices.Disable(record);
                if (disabled != 0) failure = "Windows rejected disabling the selected input (" + disabled + ").";
            } catch (Exception error) {
                failure = error.Message; if (error is Rejected) failureCode = ((Rejected)error).Code;
            } finally {
                // Neither client disconnect nor cancellation skips compensation.
                restored = Recover(record, true);
            }
            restored["disable_result"] = disabled;
            if (disabled == 0x22 || failureCode == "restart_required") restored["restart_required"] = true;
            if (!Object.Equals(restored["ok"], true)) return restored;
            if (failure != null) {
                restored["ok"] = false; restored["code"] = disabled == 0x22 ? "restart_required" : failureCode; restored["message"] = failure;
            } else {
                restored["code"] = "restarted";
                restored["message"] = "The validated input was disabled and enabled; game adoption is checked separately.";
            }
            return restored;
        }
        Dictionary<string, object> UsbReply(RecoveryRecord record) {
            var reply = Protocol.Reply(false, "usb_reenumeration_pending", "Waiting for the same controller to return on its validated USB port.");
            reply["method"] = "usb_hub_cycle_port"; reply["root_id"] = record.root_id;
            reply["physical_root_id"] = record.usb.physical_id; reply["hub_id"] = record.usb.hub_id; reply["port_index"] = record.usb.port;
            reply["status_before"] = record.status_before; reply["problem_before"] = record.problem_before; reply["pre_needs_restart"] = record.pre_needs_restart;
            return reply;
        }
        Dictionary<string, object> RecoverUsb(RecoveryRecord record, bool waitForReturn) {
            var reply = UsbReply(record);
            var elapsed = System.Diagnostics.Stopwatch.StartNew();
            do {
                try {
                    if (usb == null) throw new Rejected("usb_not_supported", "USB port verification is unavailable.");
                    // Recovery is read-only: never replay a cycle or enable a
                    // replacement device after service/client interruption.
                    usb.Verify(record); devices.Verify(record);
                    DeviceObservation state = devices.Observe(record);
                    reply["status"] = state.Status; reply["problem"] = state.Problem;
                    LastRecoveryRestartRequired = state.NeedsRestart; reply["restart_required"] = state.NeedsRestart;
                    if (!state.Healthy) throw new Rejected(state.NeedsRestart ? "restart_required" : "usb_reenumeration_pending",
                        state.NeedsRestart ? "The controller is present, but Windows requires a system restart." : "The controller input has not finished starting.");
                    // Require the exact selected HID tree to be back, not merely
                    // its MI_00 devnode with temporarily missing descendants.
                    RecoveryRecord current = Identity.Capture(usb.LoadInput(record));
                    if (!String.Equals(current.root_id, record.root_id, StringComparison.OrdinalIgnoreCase) || current.root_signature != record.root_signature)
                        throw new Rejected("usb_identity_changed", "The returning input does not match the original controller.");
                    Node currentInput = usb.LoadInput(record);
                    reply["new_input_id"] = currentInput.Id; reply["new_input_parent_id"] = currentInput.Parent.Id;
                    DeviceObservation input = usb.ObserveInput(record);
                    reply["input_status"] = input.Status; reply["input_problem"] = input.Problem;
                    LastRecoveryRestartRequired = input.NeedsRestart; reply["restart_required"] = input.NeedsRestart;
                    if (!input.Healthy) throw new Rejected(input.NeedsRestart ? "restart_required" : "usb_input_not_ready",
                        input.NeedsRestart ? "The controller input requires a system restart." : "The selected HID or input parent has not finished starting.");
                    RecoveryRecord completed = DurableJournal.Copy(record); completed.phase = "completed"; store.Save(completed);
                    LastRecoveryError = null; LastRecoveryRestartRequired = false;
                    reply.Remove("recovery_error");
                    reply["ok"] = true; reply["code"] = "enabled"; reply["message"] = "The original controller and selected input are present and healthy.";
                    return reply;
                } catch (Exception error) {
                    LastRecoveryError = error.Message; reply["message"] = error.Message;
                    reply["recovery_error"] = error is Rejected ? ((Rejected)error).Code : "recovery_failed";
                }
                if (!waitForReturn || elapsed.ElapsedMilliseconds >= 15000 || LastRecoveryRestartRequired) break;
                System.Threading.Thread.Sleep(200);
            } while (true);
            return reply;
        }
        Dictionary<string, object> RestartUsb(RecoveryRecord record) {
            store.Save(record); // Commit the port binding before the sole cycle.
            UsbCycleResult result = null; string error = null, code = "usb_cycle_failed";
            try {
                devices.Verify(record);
                result = usb.Cycle(record); // One native request; never retried.
                if (!result.Success) error = "Windows did not confirm the selected USB port cycle.";
            } catch (Exception failure) { error = failure.Message; if (failure is Rejected) code = ((Rejected)failure).Code; }
            // A cycle is one complete hub operation; unlike disable/enable it
            // has no complementary enable call. Only observe after acceptance.
            if (result != null && result.Success) System.Threading.Thread.Sleep(500);
            var reply = RecoverUsb(record, result != null && result.Success);
            reply["cycle_attempted"] = result != null;
            if (result != null) {
                reply["cycle_win32_error"] = result.Win32Error; reply["cycle_usb_status"] = result.UsbStatus; reply["cycle_bytes"] = result.Bytes;
            }
            if (!Object.Equals(reply["ok"], true)) return reply;
            if (error != null) { reply["ok"] = false; reply["code"] = code; reply["message"] = error; }
            else { reply["code"] = "restarted"; reply["message"] = "The selected controller USB port was cycled and its input returned; game adoption is checked separately."; }
            return reply;
        }
    }
}
