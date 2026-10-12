using System;
using System.Collections.Generic;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace DSBridge.HotSwitch {
    internal sealed class Rejected : Exception {
        internal readonly string Code;
        internal Rejected(string code, string message) : base(message) { Code = code; }
    }
    internal sealed class Request {
        internal string Operation, DeviceId;
    }
    internal static class Protocol {
        internal const int MaxBytes = 2048;
        internal const string Version = "1.0.6";
        internal static readonly Regex Id = new Regex(@"^HID\\[^\\\x00-\x20\x7f]{1,240}\\[^\\\x00-\x20\x7f]{1,240}$", RegexOptions.IgnoreCase);
        internal static JavaScriptSerializer Json() { return new JavaScriptSerializer { MaxJsonLength = MaxBytes, RecursionLimit = 4 }; }
        internal static Request Parse(string line) {
            if (line == null || line.Length == 0 || line.Length > MaxBytes) throw new Rejected("invalid_request", "Request size is invalid.");
            Dictionary<string, object> data;
            try { data = Json().DeserializeObject(line) as Dictionary<string, object>; }
            catch { throw new Rejected("invalid_request", "Invalid JSON object."); }
            if (data == null) throw new Rejected("invalid_request", "Expected an object.");
            foreach (string key in data.Keys) if (key != "version" && key != "operation" && key != "device_id") throw new Rejected("invalid_request", "Unknown field.");
            object version, operation, device;
            if (!data.TryGetValue("version", out version) || !(version is int) || (int)version != 1 ||
                !data.TryGetValue("operation", out operation) || !(operation is string))
                throw new Rejected("invalid_request", "Unsupported protocol.");
            string op = (string)operation;
            if (op != "status" && op != "restart") throw new Rejected("invalid_operation", "Only status and restart are supported.");
            data.TryGetValue("device_id", out device);
            if (op == "status") {
                if (device != null) throw new Rejected("invalid_request", "Status takes no device.");
            } else if (!(device is string) || !Id.IsMatch((string)device)) {
                throw new Rejected("invalid_device", "Expected one exact physical HID instance ID.");
            }
            return new Request { Operation = op, DeviceId = device as string };
        }
        internal static Dictionary<string, object> Reply(bool ok, string code, string message) {
            return new Dictionary<string, object> { {"ok", ok}, {"code", code}, {"message", message},
                {"api_version", 1}, {"version", Version}, {"restart_required", false} };
        }
    }
    internal sealed class Node {
        internal string Id, Service, ClassGuid;
        internal string[] Compatible = new string[0];
        internal readonly List<Node> Children = new List<Node>();
        internal Node Parent;
    }
    internal static class Policy {
        // CM_DRP_CLASSGUID is the device setup class, not GUID_DEVINTERFACE_HID.
        internal const string HidClass = "{745a17a0-74d3-11d0-b6fe-00a0c90f57da}";
        const string XusbClass = "{d61ca365-5af4-4486-998b-9db4734c6ca3}";
        static bool Eq(string a, string b) { return String.Equals(a, b, StringComparison.OrdinalIgnoreCase); }
        static bool HasId(Node node, string value) {
            foreach (string id in node.Compatible) if (Eq(id, value)) return true;
            return false;
        }
        internal static bool Gamepad(Node n) {
            if (!Eq(n.ClassGuid, HidClass)) return false;
            bool pad = false;
            foreach (string s in n.Compatible) {
                if (Regex.IsMatch(s, @"^HID_DEVICE_UP:0001_U:000[26]$", RegexOptions.IgnoreCase) ||
                    Eq(s, "HID_DEVICE_SYSTEM_KEYBOARD") || Eq(s, "HID_DEVICE_SYSTEM_MOUSE")) return false;
                if (Regex.IsMatch(s, @"^HID_DEVICE_UP:0001_U:000[45]$", RegexOptions.IgnoreCase)) pad = true;
            }
            return pad;
        }
        internal static Node Select(Node leaf) {
            if (leaf == null || !Protocol.Id.IsMatch(leaf.Id) || !Gamepad(leaf) || leaf.Children.Count != 0)
                throw new Rejected("unsupported_device", "The selected HID is not a unique gamepad input.");
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            for (Node n = leaf; n != null; n = n.Parent) {
                if (!seen.Add(n.Id) || seen.Count > 32) throw new Rejected("invalid_tree", "Invalid device ancestry.");
                if (Regex.IsMatch(n.Service ?? "", @"^(vigembus|usbip2_ude|vhidmini|vjoy|vjoyhid|vhf)$", RegexOptions.IgnoreCase) ||
                    Regex.IsMatch(n.Id, @"^(ROOT\\(VIGEM|VJOY|VHID)|SWD\\)", RegexOptions.IgnoreCase))
                    throw new Rejected("virtual_device", "Virtual and software devices are not restart targets.");
            }
            Match receiver = Regex.Match(leaf.Id, @"^HID\\VID_37D7&PID_2502&IG_([0-9A-F]{2})\\[^\\]+$", RegexOptions.IgnoreCase);
            if (receiver.Success) {
                Node input = leaf.Parent, root = input == null ? null : input.Parent;
                string group = receiver.Groups[1].Value;
                if (input == null || root == null ||
                    !Regex.IsMatch(input.Id, @"^USB\\VID_37D7&PID_2502&IG_" + group + @"\\[^\\]+$", RegexOptions.IgnoreCase) ||
                    !Regex.IsMatch(root.Id, @"^USB\\VID_37D7&PID_2502&MI_00\\[^\\]+$", RegexOptions.IgnoreCase) ||
                    input.Children.Count != 1 || root.Children.Count != 1 || input.Children[0] != leaf || root.Children[0] != input ||
                    !Eq(input.Service, "HidUsb") || !Eq(root.Service, "xusb22") ||
                    !Eq(input.ClassGuid, HidClass) || !Eq(root.ClassGuid, XusbClass) ||
                    !HasId(input, @"USB\Class_03") || !HasId(root, @"USB\MS_COMP_XUSB10"))
                    throw new Rejected("shared_branch", "The APEX game-input branch is not exclusive.");
                // Require the real composite USB parent, but never restart it.
                if (root.Parent == null || !Eq(root.Parent.Service, "usbccgp") || !Regex.IsMatch(root.Parent.Id, @"^USB\\VID_37D7&PID_2502\\[^\\]+$", RegexOptions.IgnoreCase))
                    throw new Rejected("invalid_tree", "The APEX USB parent does not match.");
                return root;
            }
            Node profile = leaf.Parent;
            if (profile != null && Regex.IsMatch(profile.Id,
                @"^BTHENUM\\\{00001124-0000-1000-8000-00805F9B34FB\}_VID&[0-9A-F]{8}_PID&[0-9A-F]{4}\\[^\\]+$", RegexOptions.IgnoreCase)) {
                if (profile.Children.Count != 1 || profile.Children[0] != leaf)
                    throw new Rejected("shared_branch", "The Bluetooth HID profile includes other inputs.");
                Match leafPair = Regex.Match(leaf.Id, @"VID[_&](?:[0-9A-F]{4})?([0-9A-F]{4})[_&]PID[_&]([0-9A-F]{4})", RegexOptions.IgnoreCase);
                Match parentPair = Regex.Match(profile.Id, @"VID[_&](?:[0-9A-F]{4})?([0-9A-F]{4})[_&]PID[_&]([0-9A-F]{4})", RegexOptions.IgnoreCase);
                if (!leafPair.Success || !parentPair.Success || !Eq(leafPair.Groups[1].Value, parentPair.Groups[1].Value) ||
                    !Eq(leafPair.Groups[2].Value, parentPair.Groups[2].Value) ||
                    !Eq(profile.Service, "HidBth") || !Eq(profile.ClassGuid, HidClass) ||
                    !HasId(profile, @"BTHENUM\{00001124-0000-1000-8000-00805F9B34FB}") || profile.Parent == null ||
                    !Regex.IsMatch(profile.Parent.Id, @"^BTHENUM\\DEV_[0-9A-F]{12}\\[^\\]+$", RegexOptions.IgnoreCase))
                    throw new Rejected("invalid_tree", "Not an exclusive classic Bluetooth HID profile.");
                return profile;
            }
            throw new Rejected("unsupported_device", "This physical game-input transport is not supported by the restart helper.");
        }
    }
}
