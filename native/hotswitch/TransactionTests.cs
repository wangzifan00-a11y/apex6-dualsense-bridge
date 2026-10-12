using System;
using System.Collections.Generic;

namespace DSBridge.HotSwitch {
    internal static class TransactionTests {
        sealed class Store : IRecoveryStore {
            internal RecoveryRecord Record;
            internal int Writes, FailWrite;
            public RecoveryRecord Load() { return Record == null ? null : DurableJournal.Copy(Record); }
            public void Save(RecoveryRecord record) {
                Writes++;
                if (Writes == FailWrite) throw new InvalidOperationException("Injected disk failure");
                Record = DurableJournal.Copy(record);
            }
        }
        sealed class Ops : IDeviceOperations {
            internal Node Leaf, Root;
            internal int Prepares, Disables, Enables;
            internal uint DisableResult, EnableResult, ExtraStatus, ForcedProblem;
            internal uint BeforeStatus = 8;
            internal bool ThrowDisable, ThrowEnable, Absent, StayDisabled, Disabled, LeaveEnabled;
            internal Action BeforeDisable;
            internal Ops(Node leaf) { Leaf = leaf; Root = Policy.Select(leaf); }
            public RecoveryRecord Prepare(string device) {
                Prepares++;
                RecoveryRecord record = Identity.Capture(Leaf);
                record.status_before = BeforeStatus; record.pre_needs_restart = (BeforeStatus & 0x100) != 0;
                return record;
            }
            public void Verify(RecoveryRecord record) {
                if (Absent) throw new Rejected("device_absent", "Injected unplug");
                Identity.Verify(record, Root);
            }
            public uint Disable(RecoveryRecord record) {
                Disables++; if (BeforeDisable != null) BeforeDisable();
                if (!LeaveEnabled) {Disabled = true; Root.Children.Clear();}
                if (ThrowDisable) throw new InvalidOperationException("Injected disable failure");
                return DisableResult;
            }
            public uint Enable(RecoveryRecord record) {
                Enables++;
                if (ThrowEnable) throw new InvalidOperationException("Injected enable failure");
                if (EnableResult == 0 && !StayDisabled) Disabled = false;
                return EnableResult;
            }
            public DeviceObservation Observe(RecoveryRecord record) { return new DeviceObservation {
                Status = (Disabled ? 0x400u : 8u) | ExtraStatus, Problem = ForcedProblem != 0 ? ForcedProblem : (Disabled ? 22u : 0u)}; }
        }
        static void Check(bool value, string label, ref int count) { count++; if (!value) throw new Exception("Recovery test failed: " + label); }
        static bool Ok(Dictionary<string, object> reply) { return reply != null && Object.Equals(reply["ok"], true); }
        internal static int Run(Func<Node> make) {
            int count = 0;
            Check(Devices.DisableFlags == 5 && (Devices.DisableFlags & 10) == 0, "independent SDK flag literal 5, no hardware/persist", ref count);
            var store = new Store(); var ops = new Ops(make()); var tx = new SwitchTransaction(store, ops);
            bool armedBeforeDisable = false;
            ops.BeforeDisable = delegate {armedBeforeDisable = store.Record != null && store.Record.phase == "armed";};
            var result = tx.Restart(ops.Leaf.Id);
            Check(Ok(result) && Object.Equals(result["code"], "restarted"), "normal success", ref count);
            Check(armedBeforeDisable && ops.Disables == 1 && ops.Enables == 1 && store.Record.phase == "completed", "durable before disable and finally enable", ref count);

            var marked = new DeviceObservation {Status = 0x108, Problem = 0};
            Check(marked.CanBegin && !marked.Healthy && marked.NeedsRestart, "pre restart bit permits attempt but cannot be a healthy post-state", ref count);
            Check(!new DeviceObservation {Status = 0x100, Problem = 0}.CanBegin && !new DeviceObservation {Status = 0x408, Problem = 14}.CanBegin,
                "stopped or problem-bearing roots cannot begin", ref count);
            var undefinedProblem = new DeviceObservation {Status = 8, Problem = 14};
            Check(undefinedProblem.Healthy && !undefinedProblem.NeedsRestart, "problem number has no meaning without HAS_PROBLEM", ref count);
            store = new Store(); ops = new Ops(make()) {BeforeStatus = 0x108}; tx = new SwitchTransaction(store, ops);
            result = tx.Restart(ops.Leaf.Id);
            Check(Ok(result) && Object.Equals(result["pre_needs_restart"], true) && Object.Equals(result["status_before"], 0x108u) &&
                Object.Equals(result["status"], 8u) && ops.Disables == 1 && ops.Enables == 1,
                "one attempt can naturally clear pre-existing restart bit and records both states", ref count);

            store = new Store {FailWrite = 1}; ops = new Ops(make()); tx = new SwitchTransaction(store, ops);
            bool failed = false; try {tx.Restart(ops.Leaf.Id);} catch (InvalidOperationException) {failed = true;}
            Check(failed && ops.Disables == 0 && ops.Enables == 0, "no device API after initial disk failure", ref count);

            store = new Store(); ops = new Ops(make()) {DisableResult = 23}; tx = new SwitchTransaction(store, ops);
            result = tx.Restart(ops.Leaf.Id);
            Check(!Ok(result) && ops.Enables == 1 && store.Record.phase == "completed", "failed disable still enables", ref count);
            store = new Store(); ops = new Ops(make()) {ThrowDisable = true}; tx = new SwitchTransaction(store, ops);
            result = tx.Restart(ops.Leaf.Id);
            Check(!Ok(result) && ops.Enables == 1 && store.Record.phase == "completed", "throwing disable still enables", ref count);

            store = new Store(); ops = new Ops(make()) {EnableResult = 23}; tx = new SwitchTransaction(store, ops);
            result = tx.Restart(ops.Leaf.Id);
            Check(!Ok(result) && tx.Pending() && Object.Equals(result["code"], "recovery_pending"), "failed enable keeps pending", ref count);
            int prepares = ops.Prepares;
            tx.Restart(@"HID\OTHER\TARGET");
            Check(ops.Prepares == prepares && ops.Disables == 1, "unresolved pending blocks new target", ref count);
            ops.EnableResult = 0;
            Check(Ok(tx.RecoverPending()) && !tx.Pending() && ops.Disables == 1, "recovery never disables again", ref count);

            store = new Store(); ops = new Ops(make()) {ThrowEnable = true}; tx = new SwitchTransaction(store, ops);
            Check(!Ok(tx.Restart(ops.Leaf.Id)) && tx.Pending(), "throwing enable keeps pending", ref count);
            store = new Store(); ops = new Ops(make()) {StayDisabled = true}; tx = new SwitchTransaction(store, ops);
            Check(!Ok(tx.Restart(ops.Leaf.Id)) && tx.Pending(), "API success without healthy state stays pending", ref count);

            store = new Store(); ops = new Ops(make()) {DisableResult = 0x22}; tx = new SwitchTransaction(store, ops);
            result = tx.Restart(ops.Leaf.Id);
            Check(!Ok(result) && Object.Equals(result["restart_required"], true) && ops.Enables == 1,
                "CR_NEED_RESTART from disable is retained despite successful compensation", ref count);
            store = new Store(); ops = new Ops(make()) {EnableResult = 0x22}; tx = new SwitchTransaction(store, ops);
            result = tx.Restart(ops.Leaf.Id);
            Check(!Ok(result) && Object.Equals(result["restart_required"], true) && tx.Pending(), "enable restart requirement remains pending", ref count);
            store = new Store(); ops = new Ops(make()) {ExtraStatus = 0x100}; tx = new SwitchTransaction(store, ops);
            result = tx.Restart(ops.Leaf.Id);
            Check(!Ok(result) && Object.Equals(result["restart_required"], true) && tx.Pending(), "DN_NEED_RESTART is never healthy", ref count);
            store = new Store(); ops = new Ops(make()) {ForcedProblem = 0x0e, ExtraStatus = 0x400}; tx = new SwitchTransaction(store, ops);
            result = tx.Restart(ops.Leaf.Id);
            Check(!Ok(result) && Object.Equals(result["restart_required"], true) && tx.Pending(), "CM_PROB_NEED_RESTART is explicit", ref count);

            store = new Store(); ops = new Ops(make()) {ExtraStatus = 0x100}; store.Save(Identity.Capture(ops.Leaf));
            tx = new SwitchTransaction(store, ops);
            result = tx.RecoverPending(); tx.RecoverPending(); tx.RecoverPending();
            Check(!Ok(result) && tx.Pending() && tx.LastRecoveryRestartRequired && ops.Enables == 0 && ops.Disables == 0 &&
                tx.LastRecoveryError.Contains("input is enabled"), "enabled reboot-pending input is observed without repeated enable", ref count);
            ops.ExtraStatus = 0;
            Check(Ok(tx.RecoverPending()) && !tx.Pending() && !tx.LastRecoveryRestartRequired && ops.Enables == 0,
                "healthy state after reboot completes without enable", ref count);
            store = new Store(); ops = new Ops(make()) {LeaveEnabled = true}; tx = new SwitchTransaction(store, ops);
            Check(Ok(tx.Restart(ops.Leaf.Id)) && ops.Enables == 1 && ops.Disables == 1,
                "accepted restart finally still enables even if node already reports started", ref count);

            store = new Store {FailWrite = 2}; ops = new Ops(make()); tx = new SwitchTransaction(store, ops);
            Check(!Ok(tx.Restart(ops.Leaf.Id)) && tx.Pending() && !ops.Disabled, "completed journal failure keeps replayable intent", ref count);
            Check(Ok(new SwitchTransaction(store, ops).RecoverPending()) && ops.Disables == 1, "idempotent replay after completion write failed", ref count);

            store = new Store(); ops = new Ops(make()); store.Save(Identity.Capture(ops.Leaf));
            ops.Root.Children.Clear(); ops.Disabled = true;
            tx = new SwitchTransaction(store, ops);
            Check(Ok(tx.RecoverPending()) && ops.Prepares == 0 && ops.Disables == 0 && ops.Enables == 1,
                "service crash replay works without HID children", ref count);

            store = new Store(); ops = new Ops(make()); store.Save(Identity.Capture(ops.Leaf)); ops.Absent = true;
            tx = new SwitchTransaction(store, ops);
            Check(!Ok(tx.RecoverPending()) && tx.Pending() && ops.Enables == 0, "unplug retains pending", ref count);
            ops.Absent = false; ops.Root.Children.Clear(); ops.Disabled = true;
            Check(Ok(tx.RecoverPending()) && !tx.Pending() && ops.Enables == 1, "same root return restores without old HID", ref count);

            foreach (string mutate in new string[] {"parent", "service", "class", "hardware", "extra"}) {
                store = new Store(); ops = new Ops(make()); store.Save(Identity.Capture(ops.Leaf));
                if (mutate == "parent") ops.Root.Parent.Id += "CHANGED";
                if (mutate == "service") ops.Root.Service = "BTHUSB";
                if (mutate == "class") ops.Root.ClassGuid = "{4d36e96b-e325-11ce-bfc1-08002be10318}";
                if (mutate == "hardware") ops.Root.Compatible = new string[] {@"USB\OTHER"};
                if (mutate == "extra") ops.Root.Children.Add(new Node {Id = @"HID\KEYBOARD\OTHER"});
                tx = new SwitchTransaction(store, ops);
                Check(!Ok(tx.RecoverPending()) && tx.Pending() && ops.Enables == 0, "reject changed recovery " + mutate, ref count);
            }
            store = new Store(); ops = new Ops(make()); store.Save(Identity.Capture(ops.Leaf));
            store.Record.root_id = @"USB\ROOT_HUB30\SYSTEM";
            failed = false; try {new SwitchTransaction(store, ops).RecoverPending();} catch (Rejected) {failed = true;}
            Check(failed && ops.Enables == 0, "journal root cannot authorize hub", ref count);

            store = new Store(); ops = new Ops(make()); store.Save(Identity.Capture(ops.Leaf));
            ops.Root.Id = ops.Root.Id.ToLowerInvariant(); ops.Root.Parent.Id = ops.Root.Parent.Id.ToLowerInvariant();
            ops.Root.Children.Clear();
            Check(Ok(new SwitchTransaction(store, ops).RecoverPending()), "case-only Windows instance identity is equivalent", ref count);

            var latest = "new-version"; var old0 = "known-1.0.0"; var old1 = "known-1.0.1";
            Check(Installer.ClassifyExisting(old1, "LocalSystem", 16, 2, latest, old0, old1), "both known predecessor versions accepted", ref count);
            return count;
        }
    }
}
