using System;
using System.Collections.Generic;
using System.Reflection;
using System.ServiceProcess;

[assembly: AssemblyTitle("DSBridgeHotSwitch")]
[assembly: AssemblyDescription("Restricted physical game-input restart service")]
[assembly: AssemblyVersion("1.0.6.0")]
[assembly: AssemblyFileVersion("1.0.6.0")]

namespace DSBridge.HotSwitch {
    internal static class Program {
        static int checks;
        static void Assert(bool passed, string description) { checks++; if (!passed) throw new Exception("Self-test failed: " + description); }
        static void Reject(Action action, string description) {
            bool rejected = false; try { action(); } catch (Rejected) { rejected = true; }
            Assert(rejected, description);
        }
        static Node Receiver() {
            var composite = new Node { Id = @"USB\VID_37D7&PID_2502\TEST", Service = "usbccgp" };
            var root = new Node { Id = @"USB\VID_37D7&PID_2502&MI_00\TEST", Service = "xusb22", Parent = composite,
                ClassGuid = "{d61ca365-5af4-4486-998b-9db4734c6ca3}", Compatible = new string[] {@"USB\MS_COMP_XUSB10"} };
            var input = new Node { Id = @"USB\VID_37D7&PID_2502&IG_01\TEST", Service = "HidUsb", Parent = root,
                ClassGuid = "{745a17a0-74d3-11d0-b6fe-00a0c90f57da}", Compatible = new string[] {@"USB\Class_03"} };
            var leaf = new Node { Id = @"HID\VID_37D7&PID_2502&IG_01\TEST", ClassGuid = "{745a17a0-74d3-11d0-b6fe-00a0c90f57da}",
                Compatible = new string[] {"HID_DEVICE_SYSTEM_GAME", "HID_DEVICE_UP:0001_U:0005"}, Parent = input };
            composite.Children.Add(root); root.Children.Add(input); input.Children.Add(leaf); return leaf;
        }
        static Node Bluetooth() {
            var physical = new Node { Id = @"BTHENUM\DEV_001122334455\TEST" };
            var profile = new Node { Id = @"BTHENUM\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002045E_PID&02E0\TEST", Service = "HidBth", Parent = physical,
                ClassGuid = "{745a17a0-74d3-11d0-b6fe-00a0c90f57da}", Compatible = new string[] {@"BTHENUM\{00001124-0000-1000-8000-00805F9B34FB}"} };
            var leaf = new Node { Id = @"HID\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002045E_PID&02E0&IG_00\TEST", ClassGuid = "{745a17a0-74d3-11d0-b6fe-00a0c90f57da}",
                Compatible = new string[] {"HID_DEVICE_UP:0001_U:0004"}, Parent = profile };
            physical.Children.Add(profile); profile.Children.Add(leaf); return leaf;
        }
        static Dictionary<string, object> SelfTest() {
            Assert(Protocol.Parse("{\"version\":1,\"operation\":\"status\"}").Operation == "status", "status");
            Assert(Protocol.Parse("{\"version\":1,\"operation\":\"restart\",\"device_id\":\"HID\\\\GAMEPAD\\\\TEST\"}").DeviceId == @"HID\GAMEPAD\TEST", "restart");
            foreach (string bad in new string[] { "", "[]", "null", "{}", "{\"version\":true,\"operation\":\"status\"}",
                "{\"version\":2,\"operation\":\"status\"}", "{\"version\":1,\"operation\":\"run\"}",
                "{\"version\":1,\"operation\":\"restart\",\"device_id\":\"USB\\\\ROOT\\\\TEST\"}",
                "{\"version\":1,\"operation\":\"restart\",\"device_id\":\"HID\\\\A\\\\B\",\"root_id\":\"ROOT\"}",
                "{\"version\":1,\"operation\":\"status\",\"device_id\":\"HID\\\\A\\\\B\"}", new string('x', 2049) }) {
                string value = bad; Reject(delegate { Protocol.Parse(value); }, "invalid request");
            }
            Node leaf = Receiver(); Assert(Policy.Select(leaf) == leaf.Parent.Parent, "APEX input function selected");
            leaf = Bluetooth(); Assert(Policy.Select(leaf) == leaf.Parent, "exclusive BT profile selected");
            Node interfaceGuid = Receiver(); interfaceGuid.ClassGuid = "{4d1e55b2-f16f-11cf-88cb-001111000030}";
            Reject(delegate {Policy.Select(interfaceGuid);}, "HID interface GUID is not a setup class");
            foreach (string usage in new string[] {"HID_DEVICE_UP:0001_U:0002", "HID_DEVICE_UP:0001_U:0006", "HID_DEVICE_UP:000C_U:0001"}) {
                Node value = Receiver(); value.Compatible = new string[] {usage}; Reject(delegate { Policy.Select(value); }, "non-gamepad rejected");
            }
            Node mixed = Receiver(); mixed.Compatible = new string[] {"HID_DEVICE_UP:0001_U:0005", "HID_DEVICE_SYSTEM_KEYBOARD"}; Reject(delegate { Policy.Select(mixed); }, "mixed keyboard");
            Node extra = Receiver(); extra.Parent.Parent.Children.Add(new Node {Id = "OTHER"}); Reject(delegate {Policy.Select(extra);}, "shared USB function");
            Node group = Receiver(); group.Parent.Id = @"USB\VID_37D7&PID_2502&IG_02\TEST"; Reject(delegate {Policy.Select(group);}, "wrong IG");
            Node virt = Receiver(); virt.Parent.Parent.Parent.Service = "usbip2_ude"; Reject(delegate {Policy.Select(virt);}, "virtual ancestor");
            Node fake = Receiver(); fake.Parent.Parent.Parent.Id = @"USB\VID_1234&PID_2502\TEST"; Reject(delegate {Policy.Select(fake);}, "wrong composite");
            Node otherDriver = Receiver(); otherDriver.Parent.Parent.Service = "unverified"; Reject(delegate {Policy.Select(otherDriver);}, "wrong input driver");
            Node noXusb = Receiver(); noXusb.Parent.Parent.Compatible = new string[0]; Reject(delegate {Policy.Select(noXusb);}, "missing XUSB compatible identity");
            Node wrongBt = Bluetooth(); wrongBt.Id = @"HID\VID_0001&PID_0002\TEST"; Reject(delegate {Policy.Select(wrongBt);}, "BT identity mismatch");
            Node shared = Bluetooth(); shared.Parent.Children.Add(new Node {Id = "KEYBOARD"}); Reject(delegate {Policy.Select(shared);}, "shared BT profile");
            Node ble = Bluetooth(); ble.Parent.Id = @"BTHLEDEVICE\GAMEPAD\TEST"; Reject(delegate {Policy.Select(ble);}, "BLE unsupported");
            Node radio = Bluetooth(); radio.Parent.Service = "BTHUSB"; Reject(delegate {Policy.Select(radio);}, "radio not profile");
            Node cycle = Receiver(); cycle.Parent.Parent.Parent.Parent = cycle; Reject(delegate {Policy.Select(cycle);}, "cycle");
            Node generic = Receiver(); generic.Id = @"HID\VID_045E&PID_028E\TEST"; Reject(delegate {Policy.Select(generic);}, "unknown USB unsupported");
            string expected = "\"C:\\Program Files\\DSFeedbackBridge\\HotSwitch\\1.0.1\\DSBridgeHotSwitch.exe\" --service --owner-sid S-1-5-21-1-2-3-1001";
            string previous = expected.Replace("1.0.1", "1.0.0");
            Assert(!Installer.ClassifyExisting(expected, "LocalSystem", 16, 2, expected, previous), "same protected installation");
            Assert(Installer.ClassifyExisting(previous, "LocalSystem", 16, 2, expected, previous), "exact prior installation");
            Reject(delegate {Installer.ClassifyExisting(previous.Replace("1001", "1002"), "LocalSystem", 16, 2, expected, previous);}, "another service owner");
            Reject(delegate {Installer.ClassifyExisting(previous, "LocalService", 16, 2, expected, previous);}, "another service identity");
            Reject(delegate {Installer.ClassifyExisting(previous, "LocalSystem", 16, 3, expected, previous);}, "another startup configuration");
            Reject(delegate {Installer.ClassifyExisting(@"C:\user\evil.exe", "LocalSystem", 16, 2, expected, previous);}, "arbitrary predecessor path");
            checks += TransactionTests.Run(Receiver);
            checks += UsbPortTests.Run(Receiver);
            var reply = Protocol.Reply(true, "self_test_passed", "Pure request, topology and recovery tests passed; no device APIs or service changes were executed.");
            reply["checks"] = checks; return reply;
        }
        [STAThread] static int Main(string[] args) {
            try {
                if (args.Length == 1 && args[0] == "--self-test") { Console.WriteLine(Protocol.Json().Serialize(SelfTest())); return 0; }
                if (args.Length == 1 && args[0] == "--pipe-self-test") { Console.WriteLine(Protocol.Json().Serialize(SwitchService.PipeSelfTest())); return 0; }
                if (args.Length == 2 && args[0] == "--validate-device") {
                    if (!Protocol.Id.IsMatch(args[1])) throw new Rejected("invalid_device", "Expected one exact physical HID instance ID.");
                    Node root = Policy.Select(Devices.Load(args[1]));
                    var result = Protocol.Reply(true, "validated", "Read-only validation passed; no device state was changed.");
                    result["root_id"] = root.Id; Console.WriteLine(Protocol.Json().Serialize(result)); return 0;
                }
                if (args.Length == 2 && args[0] == "--validate-usb-port") {
                    if (!Protocol.Id.IsMatch(args[1])) throw new Rejected("invalid_device", "Expected one exact physical HID instance ID.");
                    RecoveryRecord record = new Devices.Operations().Prepare(args[1]);
                    UsbPortRecord port = new UsbPortOperations().Prepare(record);
                    record.usb = port; record.schema = 2; Identity.ValidateRecord(record);
                    var result = Protocol.Reply(true, "validated", "Read-only USB port binding passed; no port was cycled.");
                    result["root_id"] = record.root_id; result["physical_root_id"] = port.physical_id;
                    result["hub_id"] = port.hub_id; result["port_index"] = port.port;
                    result["usb_serial"] = port.serial; result["driver_key"] = port.driver_key;
                    Console.WriteLine(Protocol.Json().Serialize(result)); return 0;
                }
                if (args.Length == 3 && args[1] == "--owner-sid" && (args[0] == "--install" || args[0] == "--service")) {
                    string owner = Installer.ValidateOwner(args[2]);
                    if (args[0] == "--install") { Installer.Install(owner); Console.WriteLine(Protocol.Json().Serialize(Protocol.Reply(true, "installed", "The restricted helper service is installed and running."))); return 0; }
                    ServiceBase.Run(new SwitchService(owner)); return 0;
                }
                throw new Rejected("invalid_arguments", "Use --self-test or the explicit administrator installer --install --owner-sid SID.");
            } catch (Rejected error) { Console.WriteLine(Protocol.Json().Serialize(Protocol.Reply(false, error.Code, error.Message))); return 1; }
            catch (Exception error) { Console.WriteLine(Protocol.Json().Serialize(Protocol.Reply(false, "operation_failed", error.Message))); return 1; }
        }
    }
}
