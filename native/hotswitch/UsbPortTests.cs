using System;
using System.Collections.Generic;

namespace DSBridge.HotSwitch {
    internal static class UsbPortTests {
        const string Descriptor = "1201000200000040D7370225000101020301";
        sealed class Store : IRecoveryStore {
            internal RecoveryRecord Record;
            internal int Writes, FailWrite;
            public RecoveryRecord Load() { return Record == null ? null : DurableJournal.Copy(Record); }
            public void Save(RecoveryRecord record) {
                Writes++;
                if (Writes == FailWrite) throw new InvalidOperationException("Injected USB journal write failure");
                Record = DurableJournal.Copy(record);
            }
        }
        sealed class Input : IDeviceOperations {
            internal Node Leaf;
            internal uint Status = 8;
            internal int Disables, Enables;
            internal Input(Node leaf) { Leaf = leaf; }
            public RecoveryRecord Prepare(string id) {
                if (!String.Equals(id, Leaf.Id, StringComparison.OrdinalIgnoreCase)) throw new Rejected("device_absent", "Old HID instance no longer exists");
                return Identity.Capture(Leaf);
            }
            public void Verify(RecoveryRecord record) {
                if (record.schema == 2) Identity.VerifyUsbRoot(record, Policy.Select(Leaf));
                else Identity.Verify(record, Policy.Select(Leaf));
            }
            public uint Disable(RecoveryRecord record) { Disables++; throw new Exception("USB must not call disable"); }
            public uint Enable(RecoveryRecord record) { Enables++; throw new Exception("USB must not call enable"); }
            public DeviceObservation Observe(RecoveryRecord record) { return new DeviceObservation {Status = Status, Problem = 0}; }
        }
        sealed class Ports : IUsbPortOperations {
            internal Node Leaf;
            internal int Cycles, Verifies;
            internal uint InputStatus = 8, InputProblem;
            internal bool Absent, Changed, ThrowCycle, ChangedOnPrepare;
            internal Action BeforeCycle;
            internal UsbCycleResult Result = new UsbCycleResult {IoctlSucceeded = true, Bytes = 8, ReturnedPort = 3, ExpectedPort = 3};
            internal Ports(Node leaf) { Leaf = leaf; }
            public UsbPortRecord Prepare(RecoveryRecord record) {
                UsbPortRecord value = Capture(Leaf); if (ChangedOnPrepare) value.port = 4; return value;
            }
            public void Verify(RecoveryRecord record) {
                Verifies++;
                if (Absent) throw new Rejected("device_absent", "Injected absent controller");
                UsbPortRecord current = Capture(Leaf);
                if (Changed) current.port = 4;
                UsbPortPolicy.Match(record.usb, current);
            }
            public Node LoadInput(RecoveryRecord record) { return Leaf; }
            public DeviceObservation ObserveInput(RecoveryRecord record) { return new DeviceObservation {Status = InputStatus, Problem = InputProblem}; }
            public UsbCycleResult Cycle(RecoveryRecord record) {
                Verify(record); Cycles++; if (BeforeCycle != null) BeforeCycle();
                if (ThrowCycle) throw new InvalidOperationException("Injected cycle exception");
                return Result;
            }
        }
        static Node Fixture(Func<Node> make) {
            Node leaf = make(), physical = leaf.Parent.Parent.Parent;
            physical.ClassGuid = "{36fc9e60-c465-11cf-8056-444553540000}";
            physical.Parent = new Node {Id = @"USB\ROOT_HUB30\EXAMPLE", Service = "USBHUB3", ClassGuid = "{36fc9e60-c465-11cf-8056-444553540000}"};
            physical.Parent.Children.Add(physical);
            physical.Children.Add(new Node {Id = @"USB\VID_37D7&PID_2502&MI_01\TEST", Service = "HidUsb", Parent = physical});
            return leaf;
        }
        static UsbPortRecord Capture(Node leaf) {
            Node physical = UsbPortPolicy.Physical(Policy.Select(leaf));
            return new UsbPortRecord {physical_id = physical.Id, physical_signature = Identity.Ancestors(physical),
                hub_id = physical.Parent.Id, hub_signature = Identity.Ancestors(physical.Parent),
                driver_key = @"{36fc9e60-c465-11cf-8056-444553540000}\0007", port = 3, serial = "TEST", descriptor = Descriptor};
        }
        static bool Ok(Dictionary<string, object> value) { return value != null && Object.Equals(value["ok"], true); }
        static void Check(bool result, string label, ref int count) { count++; if (!result) throw new Exception("USB test failed: " + label); }
        static void Reject(Action action, string label, ref int count) {
            bool rejected = false; try { action(); } catch (Rejected) { rejected = true; }
            Check(rejected, label, ref count);
        }
        internal static int Run(Func<Node> make) {
            int count = 0;
            Check(UsbPortOperations.CyclePort == ((0x22u << 16) | (273u << 2)) && UsbPortOperations.CyclePort != 0x22044c,
                "SDK port-cycle constant and never RESET_HUB", ref count);
            Check(UsbPortOperations.ConnectionInformation == 0x220448 && UsbPortOperations.DriverKeyName == 0x220420 &&
                UsbPortOperations.DescriptorRequest == 0x220410 && UsbPortOperations.NodeInformation == 0x220408,
                "independent SDK query constants", ref count);
            var success = new UsbCycleResult {IoctlSucceeded = true, Bytes = 8, ExpectedPort = 2, ReturnedPort = 2};
            Check(success.Success, "both native layers success", ref count);
            success.UsbStatus = 0xc0000001; Check(!success.Success, "USBD failure is not native success", ref count);
            success.UsbStatus = 0; success.IoctlSucceeded = false; Check(!success.Success, "Win32 false is failure", ref count);
            success.IoctlSucceeded = true; success.Bytes = 4; Check(!success.Success, "short output fails", ref count);
            success.Bytes = 8; success.ReturnedPort = 3; Check(!success.Success, "wrong output port fails", ref count);
            byte[] connection = new byte[35];
            for (int i = 0; i < 18; i++) connection[4 + i] = Convert.ToByte(Descriptor.Substring(i * 2, 2), 16);
            // Literal packed offsets independently assert SDK ABI, no Marshal defaults.
            connection[24] = 0; connection[31] = 1;
            Check(UsbPortOperations.DeviceDescriptor(connection)[16] == 3, "packed descriptor and status offsets", ref count);
            connection[24] = 1; Reject(delegate {UsbPortOperations.DeviceDescriptor(connection);}, "reject connected hub", ref count);
            connection[24] = 0; connection[31] = 0; Reject(delegate {UsbPortOperations.DeviceDescriptor(connection);}, "reject disconnected port", ref count);
            connection[31] = 1; connection[12] = 1; Reject(delegate {UsbPortOperations.DeviceDescriptor(connection);}, "reject other VID", ref count);
            byte[] paddedKey = new byte[22]; UsbPortOperations.Put32(paddedKey, 0, 3); UsbPortOperations.Put32(paddedKey, 4, 22);
            Buffer.BlockCopy(System.Text.Encoding.Unicode.GetBytes("KEY\0\0\0\0"), 0, paddedKey, 8, 14);
            Check(UsbPortOperations.ParseDriverKey(paddedKey, 22, 3) == "KEY", "driver key permits SDK zero WCHAR padding", ref count);
            paddedKey[20] = 65;
            Reject(delegate {UsbPortOperations.ParseDriverKey(paddedKey, 22, 3);}, "driver key rejects hidden nonzero suffix", ref count);
            paddedKey[20] = 0;
            Reject(delegate {UsbPortOperations.ParseDriverKey(paddedKey, 20, 3);}, "driver key requires exact declared length", ref count);
            byte[] selectedInfo = new byte[35]; UsbPortOperations.Put32(selectedInfo, 0, 7); selectedInfo[31] = 1;
            for (int i = 0; i < 18; i++) selectedInfo[4 + i] = Convert.ToByte(Descriptor.Substring(i * 2, 2), 16);
            int selectedQueries = 0;
            var selected = new UsbPortRecord {port = 7, driver_key = "KEY"};
            UsbPortOperations.BindSelected(selected, 12,
                delegate(uint port) {if (port != 7) throw new Exception("Unrelated empty port queried"); selectedQueries++; return selectedInfo;},
                delegate(uint port) {if (port != 7) throw new Exception("Unrelated driver key queried"); selectedQueries++; return "KEY";},
                delegate(uint port, byte index) {if (port != 7 || index != 3) throw new Exception("Unrelated serial queried"); selectedQueries++; return "RAW SERIAL";});
            Check(selectedQueries == 3 && selected.serial == "RAW SERIAL", "only selected port queried even with unrelated empty ports", ref count);
            UsbPortOperations.Put32(selectedInfo, 0, 0); selectedInfo[31] = 0;
            Reject(delegate {UsbPortOperations.ValidateConnectionIndex(selectedInfo, 35, 7, 7);}, "selected empty or wrong echoed index still rejected", ref count);
            UsbPortOperations.Put32(selectedInfo, 0, 7); selectedInfo[31] = 1;
            Reject(delegate {UsbPortOperations.BindSelected(selected, 12, delegate(uint port) {return selectedInfo;},
                delegate(uint port) {return "OTHER KEY";}, delegate(uint port, byte index) {return "RAW SERIAL";});}, "selected driver key mismatch still rejected", ref count);

            Node leaf = Fixture(make); var store = new Store(); var input = new Input(leaf); var ports = new Ports(leaf);
            var tx = new SwitchTransaction(store, input, ports); bool durable = false;
            ports.BeforeCycle = delegate {durable = store.Record != null && store.Record.phase == "armed" && store.Record.schema == 2;};
            var result = tx.Restart(leaf.Id);
            Check(Ok(result) && Object.Equals(result["method"], "usb_hub_cycle_port") && Object.Equals(result["root_id"], leaf.Parent.Parent.Id), "USB route keeps selected input root", ref count);
            Check(durable && ports.Cycles == 1 && input.Disables == 0 && input.Enables == 0 && store.Record.phase == "completed", "durable binding then one cycle, no CM changes", ref count);
            Check(store.Record.usb.physical_id == leaf.Parent.Parent.Parent.Id && store.Record.usb.port == 3, "journal binds full physical controller port", ref count);
            Check(DurableJournal.Copy(store.Record).schema == 2, "schema two roundtrip", ref count);
            var serialRecord = DurableJournal.Copy(store.Record); serialRecord.usb.serial = "Raw serial with spaces";
            Check(DurableJournal.Copy(serialRecord).usb.serial == "Raw serial with spaces", "raw USB serial need not equal PnP instance suffix", ref count);
            var legacy = Identity.Capture(leaf); Check(DurableJournal.Copy(legacy).schema == 1, "legacy schema one roundtrip", ref count);
            string json = Protocol.Json().Serialize(store.Record);
            Reject(delegate {DurableJournal.Parse(json.Replace("\"port\":3", "\"port\":3,\"command\":\"bad\""));}, "nested unknown operation refused", ref count);
            var wrongPort = DurableJournal.Copy(store.Record); wrongPort.usb.port = 0;
            Reject(delegate {Identity.ValidateRecord(wrongPort);}, "port zero refused", ref count);
            wrongPort.usb.port = 256; Reject(delegate {Identity.ValidateRecord(wrongPort);}, "port overflow refused", ref count);
            wrongPort.usb.port = 3; wrongPort.usb.physical_id = wrongPort.usb.hub_id;
            Reject(delegate {Identity.ValidateRecord(wrongPort);}, "hub cannot be journal target", ref count);

            store = new Store {FailWrite = 1}; input = new Input(leaf); ports = new Ports(leaf); tx = new SwitchTransaction(store, input, ports);
            bool failed = false; try {tx.Restart(leaf.Id);} catch (InvalidOperationException) {failed = true;}
            Check(failed && ports.Cycles == 0, "journal failure prevents cycle", ref count);
            store = new Store(); input = new Input(leaf); ports = new Ports(leaf) {ThrowCycle = true}; tx = new SwitchTransaction(store, input, ports);
            result = tx.Restart(leaf.Id);
            Check(!Ok(result) && ports.Cycles == 1 && input.Enables == 0 && store.Record.phase == "completed", "cycle exception observes healthy input without retry or enable", ref count);
            store = new Store(); input = new Input(leaf); ports = new Ports(leaf); ports.Result.UsbStatus = 0xc0000001; tx = new SwitchTransaction(store, input, ports);
            result = tx.Restart(leaf.Id);
            Check(!Ok(result) && Object.Equals(result["cycle_usb_status"], 0xc0000001u) && store.Record.phase == "completed", "USBD failure never reported as restarted", ref count);

            store = new Store(); input = new Input(leaf); ports = new Ports(leaf); ports.Result.IoctlSucceeded = false;
            ports.BeforeCycle = delegate {ports.Absent = true;}; tx = new SwitchTransaction(store, input, ports);
            result = tx.Restart(leaf.Id);
            Check(!Ok(result) && tx.Pending() && ports.Cycles == 1, "unplug stays pending", ref count);
            tx = new SwitchTransaction(store, input, ports); tx.RecoverPending(); tx.RecoverPending();
            Check(ports.Cycles == 1 && input.Enables == 0 && tx.Pending(), "service replay only observes, never another cycle", ref count);
            result = tx.Restart(leaf.Id);
            Check(!Ok(result) && ports.Cycles == 1, "pending request blocks new cycle", ref count);
            ports.Absent = false; ports.Changed = true; result = tx.RecoverPending();
            Check(!Ok(result) && tx.Pending() && ports.Cycles == 1, "replacement port fails closed", ref count);
            ports.Changed = false; result = tx.RecoverPending();
            Check(Ok(result) && !tx.Pending() && ports.Cycles == 1 && input.Enables == 0, "same controller return clears pending without API mutation", ref count);
            store.Record.phase = "armed"; ports.InputStatus = 0;
            result = tx.RecoverPending();
            Check(!Ok(result) && tx.Pending() && ports.Cycles == 1, "present HID not yet started cannot complete recovery", ref count);
            ports.InputStatus = 0x408; ports.InputProblem = 10;
            Check(!Ok(tx.RecoverPending()) && tx.Pending(), "HID or IG problem flag cannot complete recovery", ref count);
            ports.InputStatus = 0x108; ports.InputProblem = 0;
            Check(!Ok(tx.RecoverPending()) && tx.LastRecoveryRestartRequired && tx.Pending(), "HID or IG restart flag cannot complete recovery", ref count);
            ports.InputStatus = 8;
            Check(Ok(tx.RecoverPending()) && !tx.Pending() && ports.Cycles == 1, "input readiness clears pending without cycle replay", ref count);

            store = new Store(); input = new Input(leaf) {Status = 0x108}; ports = new Ports(leaf); tx = new SwitchTransaction(store, input, ports);
            result = tx.Restart(leaf.Id);
            Check(!Ok(result) && tx.Pending() && tx.LastRecoveryRestartRequired && ports.Cycles == 1, "post restart flag retains pending", ref count);
            tx.RecoverPending(); Check(ports.Cycles == 1 && input.Enables == 0, "restart-needed post state is observation only", ref count);
            input.Status = 8; Check(Ok(tx.RecoverPending()) && !tx.Pending(), "system restart state clearing completes pending", ref count);
            Node other = Fixture(make); other.Parent.Parent.Parent.Children.Add(new Node {Id = @"USB\VID_1234&PID_5678&MI_00\OTHER"});
            Reject(delegate {UsbPortPolicy.Physical(Policy.Select(other));}, "unrecognized composite child rejected", ref count);
            other = Fixture(make); other.Parent.Parent.Parent.Service = "USBHUB3";
            Reject(delegate {UsbPortPolicy.Physical(other.Parent.Parent);}, "physical target cannot be hub", ref count);
            other = Fixture(make); other.Parent.Parent.Parent.Parent.Service = "usbip2_ude";
            Reject(delegate {UsbPortPolicy.Physical(other.Parent.Parent);}, "parent virtual hub rejected", ref count);
            UsbPortRecord binding = Capture(leaf), changed = Capture(leaf); changed.driver_key += "1";
            Reject(delegate {UsbPortPolicy.Match(binding, changed);}, "driver key changed", ref count);
            changed = Capture(leaf); changed.serial = "REPLACED";
            Reject(delegate {UsbPortPolicy.Match(binding, changed);}, "serial changed", ref count);
            changed = Capture(leaf); changed.hub_id += "OTHER";
            Reject(delegate {UsbPortPolicy.Match(binding, changed);}, "hub changed", ref count);
            changed = Capture(leaf); changed.descriptor = changed.descriptor.Replace("0102", "0103");
            Reject(delegate {UsbPortPolicy.Match(binding, changed);}, "descriptor changed", ref count);

            leaf = Fixture(make); string originalId = leaf.Id;
            store = new Store(); input = new Input(leaf); ports = new Ports(leaf); tx = new SwitchTransaction(store, input, ports);
            ports.BeforeCycle = delegate {
                leaf.Id = @"HID\VID_37D7&PID_2502&IG_02\NEW-INSTANCE";
                leaf.Parent.Id = @"USB\VID_37D7&PID_2502&IG_02\NEW-INSTANCE";
            };
            result = tx.Restart(originalId);
            Check(Ok(result) && Object.Equals(result["new_input_id"], leaf.Id) && Object.Equals(result["new_input_parent_id"], leaf.Parent.Id),
                "IG index and instance suffix migration on the same USB root accepted", ref count);
            Check(store.Record.device_id == originalId && store.Record.phase == "completed" && ports.Cycles == 1,
                "completed protected journal retains original alias", ref count);
            ports.BeforeCycle = null;
            ports.ChangedOnPrepare = true;
            Reject(delegate {tx.Restart(originalId);}, "alias rejects port migration between validation and fresh prepare", ref count);
            Check(ports.Cycles == 1 && store.Record.device_id == originalId, "alias race cannot overwrite journal or cycle new port", ref count);
            ports.ChangedOnPrepare = false;
            result = tx.Restart(originalId);
            Check(Ok(result) && ports.Cycles == 2 && store.Record.device_id == leaf.Id,
                "explicit old-ID request resolves completed protected same-port alias", ref count);
            int previousCycles = ports.Cycles;
            Reject(delegate {tx.Restart(@"HID\VID_37D7&PID_2502&IG_03\UNRELATED");}, "unrelated absent ID has no fallback", ref count);
            Check(ports.Cycles == previousCycles, "unrelated alias rejection sends no cycle", ref count);

            leaf = Fixture(make); originalId = leaf.Id;
            store = new Store(); input = new Input(leaf); ports = new Ports(leaf); tx = new SwitchTransaction(store, input, ports);
            RecoveryRecord armed = input.Prepare(originalId); armed.usb = ports.Prepare(armed); armed.schema = 2; store.Save(armed);
            leaf.Id = @"HID\VID_37D7&PID_2502&IG_03\RECOVERED";
            leaf.Parent.Id = @"USB\VID_37D7&PID_2502&IG_03\RECOVERED";
            result = tx.RecoverPending();
            Check(Ok(result) && Object.Equals(result["new_input_id"], leaf.Id) && ports.Cycles == 0 && input.Enables == 0,
                "service startup recovers changed IG by observation only", ref count);
            RecoveryRecord preserved = DurableJournal.Copy(store.Record);
            string rootId = leaf.Parent.Parent.Id;
            leaf.Parent.Parent.Id = @"USB\VID_37D7&PID_2502&MI_00\DIFFERENT-ROOT";
            Reject(delegate {Identity.VerifyUsbRoot(preserved, leaf.Parent.Parent);}, "IG migration never permits another MI00 root", ref count);
            leaf.Parent.Parent.Id = rootId;
            string parentId = leaf.Parent.Parent.Parent.Id; leaf.Parent.Parent.Parent.Id = @"USB\VID_37D7&PID_2502\ANOTHER-PHYSICAL";
            Reject(delegate {Identity.VerifyUsbRoot(preserved, leaf.Parent.Parent);}, "IG migration never permits another physical controller", ref count);
            leaf.Parent.Parent.Parent.Id = parentId;
            leaf.Compatible = new string[] {"HID_DEVICE_UP:0001_U:0006"};
            Reject(delegate {Policy.Select(leaf);}, "replacement keyboard usage refused", ref count);
            return count;
        }
    }
}
