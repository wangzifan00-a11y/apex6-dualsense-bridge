using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace DSBridge.HotSwitch {
    internal static class Devices {
        // WinSDK cfgmgr32.h: ABSOLUTE=1 (do not query driver), UI_NOT_OK=4.
        // No HARDWARE=2 or PERSIST=8. Actual driver behavior still needs testing.
        internal const uint DisableFlags = 1 | 4;
        const uint NoNode = 13;
        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)] static extern uint CM_Locate_DevNodeW(out uint node, string id, uint flags);
        [DllImport("cfgmgr32.dll")] static extern uint CM_Get_Parent(out uint parent, uint node, uint flags);
        [DllImport("cfgmgr32.dll")] static extern uint CM_Get_Child(out uint child, uint node, uint flags);
        [DllImport("cfgmgr32.dll")] static extern uint CM_Get_Sibling(out uint sibling, uint node, uint flags);
        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)] static extern uint CM_Get_Device_IDW(uint node, StringBuilder id, uint length, uint flags);
        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)] static extern uint CM_Get_DevNode_Registry_PropertyW(uint node, uint property, out uint type, byte[] buffer, ref uint size, uint flags);
        [DllImport("cfgmgr32.dll")] static extern uint CM_Get_DevNode_Status(out uint status, out uint problem, uint node, uint flags);
        [DllImport("cfgmgr32.dll")] static extern uint CM_Disable_DevNode(uint node, uint flags);
        [DllImport("cfgmgr32.dll")] static extern uint CM_Enable_DevNode(uint node, uint flags);
        [StructLayout(LayoutKind.Sequential)] struct DevInfo { internal uint cbSize; internal Guid ClassGuid; internal uint DevInst; internal UIntPtr Reserved; }
        [StructLayout(LayoutKind.Sequential)] struct Change { internal uint cbSize, function, state, scope, profile; }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct InstallParams {
            internal uint cbSize, Flags, FlagsEx;
            internal IntPtr hwndParent, InstallMsgHandler, InstallMsgHandlerContext, FileQueue;
            internal UIntPtr ClassInstallReserved;
            internal uint Reserved;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] internal string DriverPath;
        }
        [DllImport("setupapi.dll", SetLastError = true)] static extern IntPtr SetupDiCreateDeviceInfoList(IntPtr guid, IntPtr hwnd);
        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool SetupDiOpenDeviceInfoW(IntPtr set, string id, IntPtr hwnd, uint flags, ref DevInfo data);
        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool SetupDiSetClassInstallParamsW(IntPtr set, ref DevInfo data, ref Change parameters, uint size);
        [DllImport("setupapi.dll", SetLastError = true)] static extern bool SetupDiCallClassInstaller(uint function, IntPtr set, ref DevInfo data);
        [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool SetupDiGetDeviceInstallParamsW(IntPtr set, ref DevInfo data, ref InstallParams parameters);
        [DllImport("setupapi.dll")] static extern bool SetupDiDestroyDeviceInfoList(IntPtr set);

        static string Instance(uint node) {
            var text = new StringBuilder(1024);
            if (CM_Get_Device_IDW(node, text, (uint)text.Capacity, 0) != 0) throw new Rejected("device_changed", "The input device is no longer present.");
            return text.ToString();
        }
        internal static string[] Property(uint node, uint key) {
            uint type, size = 8192; byte[] buffer = new byte[size];
            uint result = CM_Get_DevNode_Registry_PropertyW(node, key, out type, buffer, ref size, 0);
            if (result == 0x25) return new string[0]; // CR_NO_SUCH_VALUE
            if (result != 0 || size > buffer.Length || size % 2 != 0 || (type != 1 && type != 7))
                throw new Rejected("invalid_tree", "Cannot validate device properties.");
            return Encoding.Unicode.GetString(buffer, 0, (int)size).Split(new char[] {'\0'}, StringSplitOptions.RemoveEmptyEntries);
        }
        static string First(string[] values) { return values.Length > 0 ? values[0] : ""; }
        internal static Node Load(string id) {
            if (!Protocol.Id.IsMatch(id)) throw new Rejected("invalid_device", "Expected one exact HID instance.");
            return LoadTree(id);
        }
        internal static Node LoadTree(string id) {
            uint selected;
            if (CM_Locate_DevNodeW(out selected, id, 0) != 0)
                throw new Rejected("device_absent", "The selected input is not present.");
            var cache = new Dictionary<uint, Node>();
            Func<uint, Node> get = delegate(uint n) {
                Node existing;
                if (cache.TryGetValue(n, out existing)) return existing;
                var ids = new List<string>(Property(n, 2)); // Some XInput HID nodes publish usage only as hardware IDs.
                ids.AddRange(Property(n, 3));
                var item = new Node { Id = Instance(n), Service = First(Property(n, 5)), ClassGuid = First(Property(n, 9)), Compatible = ids.ToArray() };
                cache.Add(n, item); return item;
            };
            uint current = selected;
            var visited = new HashSet<uint>();
            while (true) {
                if (!visited.Add(current) || visited.Count > 32) throw new Rejected("invalid_tree", "Device ancestry is too deep.");
                Node item = get(current);
                uint child; uint result = CM_Get_Child(out child, current, 0);
                var children = new HashSet<uint>();
                while (result == 0) {
                    if (!children.Add(child) || children.Count > 256) throw new Rejected("invalid_tree", "Device branch cannot be validated.");
                    Node c = get(child); c.Parent = item; item.Children.Add(c);
                    uint next; result = CM_Get_Sibling(out next, child, 0); child = next;
                }
                if (result != NoNode) throw new Rejected("invalid_tree", "Device enumeration was incomplete.");
                uint parent; result = CM_Get_Parent(out parent, current, 0);
                if (result == NoNode) break;
                if (result != 0) throw new Rejected("invalid_tree", "Device ancestry was incomplete.");
                item.Parent = get(parent); current = parent;
            }
            Node leaf = get(selected);
            if (!String.Equals(leaf.Id, id, StringComparison.OrdinalIgnoreCase)) throw new Rejected("device_changed", "The selected input changed.");
            return leaf;
        }
        internal static uint LocateExact(string id) {
            uint node;
            if (CM_Locate_DevNodeW(out node, id, 0) != 0 || !String.Equals(Instance(node), id, StringComparison.OrdinalIgnoreCase))
                throw new Rejected("device_absent", "The validated physical USB device is not present.");
            return node;
        }
        internal static uint DwordProperty(string id, uint property) {
            uint type, size = 4; byte[] buffer = new byte[4];
            if (CM_Get_DevNode_Registry_PropertyW(LocateExact(id), property, out type, buffer, ref size, 0) != 0 || type != 4 || size != 4)
                throw new Rejected("usb_query_failed", "Cannot validate the USB port address.");
            return BitConverter.ToUInt32(buffer, 0);
        }
        internal static string TextProperty(string id, uint property) {
            string[] values = Property(LocateExact(id), property);
            if (values.Length != 1 || values[0].Length == 0) throw new Rejected("usb_query_failed", "Cannot validate the USB driver key.");
            return values[0];
        }
        internal static DeviceObservation ObserveExact(string id) {
            uint status, problem;
            if (CM_Get_DevNode_Status(out status, out problem, LocateExact(id), 0) != 0)
                throw new Rejected("status_unavailable", "The exact input devnode state is unavailable.");
            return new DeviceObservation {Status = status, Problem = problem};
        }
        static Node LoadRecoveryRoot(string id) {
            Node root = LoadTree(id);
            // Load the two descendant levels that can belong to an accepted root.
            // Ancestors are read, never passed to an enable/disable API.
            var children = new List<Node>(root.Children); root.Children.Clear();
            if (children.Count > 1) throw new Rejected("shared_branch", "Recovery root is no longer exclusive.");
            foreach (Node item in children) {
                Node child = LoadTree(item.Id);
                if (child.Parent == null || Identity.Fingerprint(child.Parent) != Identity.Fingerprint(root))
                    throw new Rejected("device_changed", "Recovery child's physical parent changed during enumeration.");
                child.Parent = root; root.Children.Add(child);
                if (child.Children.Count > 1) throw new Rejected("shared_branch", "Recovery input has unexpected descendants.");
                var grandchildren = new List<Node>(child.Children); child.Children.Clear();
                foreach (Node grandchild in grandchildren) {
                    Node leaf = LoadTree(grandchild.Id);
                    if (leaf.Parent == null || Identity.Fingerprint(leaf.Parent) != Identity.Fingerprint(child))
                        throw new Rejected("device_changed", "Recovery leaf's physical parent changed during enumeration.");
                    leaf.Parent = child; child.Children.Add(leaf);
                    if (leaf.Children.Count != 0) throw new Rejected("shared_branch", "Recovery leaf has unexpected descendants.");
                }
            }
            return root;
        }
        static uint LocateVerified(RecoveryRecord record) {
            Node root = LoadRecoveryRoot(record.root_id);
            if (record.schema == 2) Identity.VerifyUsbRoot(record, root);
            else Identity.Verify(record, root);
            uint node;
            if (CM_Locate_DevNodeW(out node, record.root_id, 0) != 0 || !String.Equals(Instance(node), record.root_id, StringComparison.OrdinalIgnoreCase))
                throw new Rejected("device_absent", "The original physical input is not currently present.");
            return node;
        }
        internal static Node LoadUsbInput(RecoveryRecord record) {
            Node root = LoadRecoveryRoot(record.root_id);
            Identity.VerifyUsbRoot(record, root);
            if (root.Children.Count != 1 || root.Children[0].Children.Count != 1)
                throw new Rejected("device_absent", "The original USB controller input has not finished enumerating.");
            Node leaf = root.Children[0].Children[0];
            Node selected = Policy.Select(leaf);
            Identity.VerifyUsbRoot(record, selected);
            return leaf;
        }
        internal sealed class Operations : IDeviceOperations {
            public RecoveryRecord Prepare(string id) {
                RecoveryRecord first = Identity.Capture(Load(id));
                RecoveryRecord second = Identity.Capture(Load(id));
                if (first.root_id != second.root_id || first.root_signature != second.root_signature)
                    throw new Rejected("device_changed", "The physical input identity changed.");
                Verify(second);
                DeviceObservation before = Observe(second);
                // DN_NEED_RESTART is diagnostic pre-state, not a documented CM
                // call prohibition. Only started roots without HAS_PROBLEM may begin;
                // the post-state still must clear this bit without us editing it.
                if (!before.CanBegin) throw new Rejected("device_not_started", "Input must already be started without a problem flag.");
                second.status_before = before.Status; second.problem_before = before.Problem;
                second.pre_needs_restart = before.NeedsRestart;
                return second;
            }
            public void Verify(RecoveryRecord record) { LocateVerified(record); }
            public uint Disable(RecoveryRecord record) {
                if (record.schema == 2) throw new Rejected("invalid_operation", "USB port journals cannot authorize CM disable.");
                uint node = LocateVerified(record), status, problem;
                if (CM_Get_DevNode_Status(out status, out problem, node, 0) != 0)
                    throw new Rejected("status_unavailable", "Input state cannot be confirmed immediately before disable.");
                if ((status & 0x400) != 0 || (status & 8) == 0)
                    throw new Rejected("device_not_started", "Input was not healthy immediately before disable.");
                return CM_Disable_DevNode(node, DisableFlags);
            }
            public uint Enable(RecoveryRecord record) {
                if (record.schema == 2) throw new Rejected("invalid_operation", "USB port journals cannot authorize CM enable.");
                return CM_Enable_DevNode(LocateVerified(record), 0);
            }
            public DeviceObservation Observe(RecoveryRecord record) {
                DeviceObservation result = null;
                for (int attempt = 0; attempt < 20; attempt++) {
                    uint node = LocateVerified(record), status, problem;
                    if (CM_Get_DevNode_Status(out status, out problem, node, 0) != 0)
                        throw new Rejected("status_unavailable", "The original input state cannot be verified.");
                    result = new DeviceObservation {Status = status, Problem = problem};
                    if (result.CanBegin) return result;
                    if (attempt < 19) Thread.Sleep(100);
                }
                return result;
            }
        }
        // Retained for diagnosis/source comparison, with no CLI or pipe route.
        // Normal switching never runs PROPCHANGE before CM disable/enable.
        internal static Dictionary<string, object> RestartViaPropertyChangeDiagnostic(string id) {
            // Re-read immediately before SetupAPI; callers never supply a root.
            Node root = Policy.Select(Load(id));
            Node second = Policy.Select(Load(id));
            if (!String.Equals(root.Id, second.Id, StringComparison.OrdinalIgnoreCase)) throw new Rejected("device_changed", "Device topology changed.");
            IntPtr set = SetupDiCreateDeviceInfoList(IntPtr.Zero, IntPtr.Zero);
            if (set == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
            try {
                var info = new DevInfo { cbSize = (uint)Marshal.SizeOf(typeof(DevInfo)) };
                if (!SetupDiOpenDeviceInfoW(set, root.Id, IntPtr.Zero, 0, ref info)) throw new Win32Exception(Marshal.GetLastWin32Error());
                if (!String.Equals(Instance(info.DevInst), root.Id, StringComparison.OrdinalIgnoreCase)) throw new Rejected("device_changed", "Device identity changed.");
                uint status, problem;
                if (CM_Get_DevNode_Status(out status, out problem, info.DevInst, 0) != 0 || problem != 0 || (status & 8) == 0)
                    throw new Rejected("device_not_started", "Input must already be started and healthy.");
                var before = new InstallParams { cbSize = (uint)Marshal.SizeOf(typeof(InstallParams)) };
                if (!SetupDiGetDeviceInstallParamsW(set, ref info, ref before)) throw new Win32Exception(Marshal.GetLastWin32Error());
                var change = new Change { cbSize = 8, function = 0x12, state = 3, scope = 1, profile = 0 };
                if (!SetupDiSetClassInstallParamsW(set, ref info, ref change, (uint)Marshal.SizeOf(typeof(Change)))) throw new Win32Exception(Marshal.GetLastWin32Error());
                // DICS_PROPCHANGE is one restart operation; never leave a device disabled.
                if (!SetupDiCallClassInstaller(0x12, set, ref info)) throw new Win32Exception(Marshal.GetLastWin32Error());
                var parameters = new InstallParams { cbSize = (uint)Marshal.SizeOf(typeof(InstallParams)) };
                if (!SetupDiGetDeviceInstallParamsW(set, ref info, ref parameters)) throw new Win32Exception(Marshal.GetLastWin32Error());
                if ((parameters.Flags & 0x180) != 0) {
                    var reboot = Protocol.Reply(false, "restart_required", "Windows requires a system restart; live switching was not confirmed.");
                    reboot["restart_required"] = true; reboot["root_id"] = root.Id;
                    reboot["flags_before"] = before.Flags; reboot["flags_after"] = parameters.Flags;
                    reboot["flags_added"] = parameters.Flags & ~before.Flags; return reboot;
                }
                var reply = Protocol.Reply(true, "restarted", "The selected game-input branch was restarted.");
                reply["root_id"] = root.Id; reply["flags_before"] = before.Flags; reply["flags_after"] = parameters.Flags;
                reply["flags_added"] = parameters.Flags & ~before.Flags; return reply;
            } finally { SetupDiDestroyDeviceInfoList(set); }
        }
    }
}
