using System;
using System.ComponentModel;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using Microsoft.Win32;

namespace DSBridge.HotSwitch {
    internal static class Installer {
        const string SystemSid = "S-1-5-18", AdminSid = "S-1-5-32-544";
        const string TrustedInstaller = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool LookupAccountSidW(string system, byte[] sid, StringBuilder name, ref uint nameLength, StringBuilder domain, ref uint domainLength, out uint use);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr OpenSCManagerW(string machine, string database, uint access);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr OpenServiceW(IntPtr manager, string name, uint access);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool ChangeServiceConfigW(IntPtr service, uint type, uint start, uint error, string path,
            string group, IntPtr tag, string dependencies, string account, string password, string display);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CreateServiceW(IntPtr manager, string name, string display, uint access, uint type, uint start, uint error, string path,
            string group, IntPtr tag, string dependencies, string account, string password);
        [DllImport("advapi32.dll")] static extern bool CloseServiceHandle(IntPtr handle);
        [StructLayout(LayoutKind.Sequential)] struct FailureActions {
            internal uint ResetPeriod;
            internal IntPtr RebootMessage, Command;
            internal uint Count;
            internal IntPtr Actions;
        }
        [StructLayout(LayoutKind.Sequential)] struct FailureAction { internal uint Type, Delay; }
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool ChangeServiceConfig2W(IntPtr service, uint level, ref FailureActions actions);
        internal static string ValidateOwner(string value) {
            SecurityIdentifier sid;
            try { sid = new SecurityIdentifier(value); }
            catch { throw new Rejected("invalid_owner", "A Windows user SID is required."); }
            if (sid.Value != value || !sid.IsAccountSid()) throw new Rejected("invalid_owner", "Owner must be an individual account SID.");
            byte[] bytes = new byte[sid.BinaryLength]; sid.GetBinaryForm(bytes, 0);
            uint names = 512, domains = 512, use; var name = new StringBuilder(512); var domain = new StringBuilder(512);
            if (!LookupAccountSidW(null, bytes, name, ref names, domain, ref domains, out use) || use != 1)
                throw new Rejected("invalid_owner", "Owner must resolve to an individual Windows user.");
            return value;
        }
        static bool Trusted(string sid) { return sid == SystemSid || sid == AdminSid || sid == TrustedInstaller; }
        static void NoReparse(string path) {
            string full = Path.GetFullPath(path);
            for (string p = full; !String.IsNullOrEmpty(p); p = Path.GetDirectoryName(p)) {
                if (!Directory.Exists(p) && !File.Exists(p)) continue;
                if ((File.GetAttributes(p) & FileAttributes.ReparsePoint) != 0) throw new Rejected("untrusted_path", "Installation paths must not contain links or reparse points.");
                if (p == Path.GetPathRoot(p)) break;
            }
        }
        internal static void Protected(string path, bool directory) {
            NoReparse(path);
            FileSystemSecurity security = directory ? (FileSystemSecurity)Directory.GetAccessControl(path) : File.GetAccessControl(path);
            if (!Trusted(security.GetOwner(typeof(SecurityIdentifier)).Value)) throw new Rejected("untrusted_path", "Installation owner is not trusted.");
            const FileSystemRights writes = FileSystemRights.WriteData | FileSystemRights.AppendData | FileSystemRights.WriteExtendedAttributes |
                FileSystemRights.WriteAttributes | FileSystemRights.Delete | FileSystemRights.DeleteSubdirectoriesAndFiles |
                FileSystemRights.ChangePermissions | FileSystemRights.TakeOwnership;
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier))) {
                // Inherit-only creator entries on the Windows Program Files parent do not authorize modifying that directory.
                if ((rule.PropagationFlags & PropagationFlags.InheritOnly) != 0) continue;
                if (rule.AccessControlType == AccessControlType.Allow && (rule.FileSystemRights & writes) != 0 && !Trusted(rule.IdentityReference.Value))
                    throw new Rejected("untrusted_path", "Installation is writable by a non-administrator.");
            }
        }
        static DirectorySecurity DirectoryAcl() {
            var acl = new DirectorySecurity(); acl.SetAccessRuleProtection(true, false); acl.SetOwner(new SecurityIdentifier(AdminSid));
            foreach (string sid in new string[] {SystemSid, AdminSid})
                acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.ReadAndExecute,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            return acl;
        }
        internal static string StateDirectory(bool create) {
            string path = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            if (String.IsNullOrEmpty(path)) throw new Rejected("invalid_install_path", "Windows Program Files is unavailable.");
            Protected(path, true);
            foreach (string segment in new string[] {"DSFeedbackBridge", "HotSwitch", "State"}) {
                path = Path.Combine(path, segment);
                if (File.Exists(path)) throw new Rejected("untrusted_path", "The fixed recovery directory is occupied by a file.");
                if (!Directory.Exists(path)) {
                    if (!create) throw new Rejected("state_unavailable", "Protected recovery storage has not been installed.");
                    Directory.CreateDirectory(path, DirectoryAcl());
                }
                Protected(path, true);
            }
            return path;
        }
        static string Hash(byte[] data) { using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(data)).Replace("-", "").ToLowerInvariant(); }
        internal static bool ClassifyExisting(string image, string account, int type, int start, string expected, params string[] previous) {
            if (!String.Equals(account, "LocalSystem", StringComparison.OrdinalIgnoreCase) || type != 0x10 || start != 2)
                throw new Rejected("service_conflict", "Existing service identity or startup configuration is not owned by this installer.");
            if (String.Equals(image, expected, StringComparison.OrdinalIgnoreCase)) return false;
            foreach (string candidate in previous) if (String.Equals(image, candidate, StringComparison.OrdinalIgnoreCase)) return true;
            throw new Rejected("service_conflict", "Existing service command does not belong to this installation and user.");
        }
        static void ChangeCommand(IntPtr scm, string command) {
            IntPtr handle = OpenServiceW(scm, SwitchService.Name, 2); // SERVICE_CHANGE_CONFIG only.
            if (handle == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            try {
                if (!ChangeServiceConfigW(handle, 0xffffffff, 0xffffffff, 0xffffffff, command, null, IntPtr.Zero, null, null, null, null))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            } finally { CloseServiceHandle(handle); }
        }
        static void StopAndWait(ServiceController control) {
            control.Refresh();
            if (control.Status == ServiceControllerStatus.Stopped) return;
            if (control.Status != ServiceControllerStatus.StopPending) control.Stop();
            control.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(20));
        }
        static void ConfigureFailureRecovery(IntPtr scm) {
            IntPtr handle = OpenServiceW(scm, SwitchService.Name, 2 | 16); // CHANGE_CONFIG + START for restart actions.
            if (handle == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            IntPtr actions = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FailureAction)) * 3);
            try {
                for (int index = 0; index < 3; index++)
                    Marshal.StructureToPtr(new FailureAction {Type = 1, Delay = new uint[] {5000, 15000, 60000}[index]},
                        IntPtr.Add(actions, index * Marshal.SizeOf(typeof(FailureAction))), false);
                // Only SCM restart actions, never a command or system reboot.
                var config = new FailureActions {ResetPeriod = 86400, Count = 3, Actions = actions};
                if (!ChangeServiceConfig2W(handle, 2, ref config)) throw new Win32Exception(Marshal.GetLastWin32Error());
            } finally { Marshal.FreeHGlobal(actions); CloseServiceHandle(handle); }
        }
        internal static void Install(string owner) {
            if (!new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator))
                throw new Rejected("administrator_required", "Run the explicitly requested installer with administrator approval.");
            owner = ValidateOwner(owner);
            StateDirectory(true);
            string programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            if (String.IsNullOrEmpty(programFiles)) throw new Rejected("invalid_install_path", "Windows Program Files is unavailable.");
            Protected(programFiles, true);
            string path = programFiles;
            foreach (string segment in new string[] { "DSFeedbackBridge", "HotSwitch", Protocol.Version }) {
                path = Path.Combine(path, segment);
                if (File.Exists(path)) throw new Rejected("untrusted_path", "Installation directory is occupied by a file.");
                if (!Directory.Exists(path)) Directory.CreateDirectory(path, DirectoryAcl());
                Protected(path, true);
            }
            string source = Assembly.GetExecutingAssembly().Location, destination = Path.Combine(path, "DSBridgeHotSwitch.exe");
            byte[] contents = File.ReadAllBytes(source);
            if (File.Exists(destination)) {
                Protected(destination, false);
                if (Hash(File.ReadAllBytes(destination)) != Hash(contents)) throw new Rejected("version_conflict", "An installation with this version already has different contents.");
            } else {
                using (var output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None)) {
                    output.Write(contents, 0, contents.Length); output.Flush(true);
                }
                Protected(destination, false);
                if (Hash(File.ReadAllBytes(destination)) != Hash(contents)) throw new Rejected("copy_failed", "Installed binary validation failed.");
            }
            string command = "\"" + destination + "\" --service --owner-sid " + owner;
            string previousPath = null, previousCommand = null, previousVersion = null;
            var approved = new System.Collections.Generic.Dictionary<string, string> {
                {"1.0.0", "866301fd530ac5d493d5646865c76205ca17655b8dfa41df1b858caa7975e8e2"},
                {"1.0.1", "ac2d87c7029ef4987371c28667ced9aebf71e36a331890ed0880521323844058"},
                {"1.0.2", "3ab2e4baf49b7dcf5b0cc49022cc21a03c3db19aa3675fc53fb427b5a1a67794"},
                {"1.0.3", "bd42adf5393f9310d9ef13c1287bff7061e5e7ecc784a38547048088ebffb94c"},
                {"1.0.4", "6ce720b9799ef631a2612d84253a4f93d5949c5ae04877bf6cb21371f91452bd"},
                {"1.0.5", "4f33f632ce63772903c4a2d9234c4dc24a402e5a4fa85b572cdb4f7cb3d90cab"}
            };
            var previousCommands = new System.Collections.Generic.List<string>();
            foreach (string version in approved.Keys)
                previousCommands.Add("\"" + Path.Combine(programFiles, "DSFeedbackBridge", "HotSwitch", version, "DSBridgeHotSwitch.exe") + "\" --service --owner-sid " + owner);
            bool upgrade = false, stopRequested = false;
            IntPtr scm = OpenSCManagerW(null, null, 3);
            if (scm == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            try {
                // LocalSystem, own-process, automatic. Default SCM ACL has no ordinary-user config rights.
                IntPtr service = CreateServiceW(scm, SwitchService.Name, "DS Feedback Bridge game-input switch", 0xF01FF, 0x10, 2, 1, command, null, IntPtr.Zero, null, null, null);
                if (service == IntPtr.Zero) {
                    int error = Marshal.GetLastWin32Error();
                    if (error != 1073) throw new Win32Exception(error);
                    using (RegistryKey key = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Services\" + SwitchService.Name, false)) {
                        if (key == null) throw new Rejected("service_conflict", "Existing service configuration cannot be read.");
                        string existing = key.GetValue("ImagePath") as string;
                        upgrade = ClassifyExisting(existing, key.GetValue("ObjectName") as string,
                            Convert.ToInt32(key.GetValue("Type", 0)), Convert.ToInt32(key.GetValue("Start", 0)), command, previousCommands.ToArray());
                        if (upgrade) foreach (string version in approved.Keys) {
                            string candidate = Path.Combine(programFiles, "DSFeedbackBridge", "HotSwitch", version, "DSBridgeHotSwitch.exe");
                            string candidateCommand = "\"" + candidate + "\" --service --owner-sid " + owner;
                            if (String.Equals(existing, candidateCommand, StringComparison.OrdinalIgnoreCase)) {
                                previousPath = candidate; previousCommand = candidateCommand; previousVersion = version;
                            }
                        }
                    }
                } else CloseServiceHandle(service);
                using (var control = new ServiceController(SwitchService.Name)) {
                    if (upgrade) {
                        Protected(Path.GetDirectoryName(previousPath), true); Protected(previousPath, false);
                        if (Hash(File.ReadAllBytes(previousPath)) != approved[previousVersion] ||
                            AssemblyName.GetAssemblyName(previousPath).Version.ToString() != previousVersion + ".0")
                            throw new Rejected("service_conflict", "The previous helper binary is not an approved predecessor.");
                    }
                    try {
                        if (upgrade) {
                            stopRequested = true; StopAndWait(control); ChangeCommand(scm, command);
                        }
                        ConfigureFailureRecovery(scm);
                        control.Refresh();
                        if (control.Status == ServiceControllerStatus.Stopped) control.Start();
                        control.WaitForStatus(ServiceControllerStatus.Running, TimeSpan.FromSeconds(20));
                    } catch {
                        if (upgrade && stopRequested) {
                            // Preserve the old protected binary and roll SCM back
                            // if the new service cannot start. Nothing is deleted.
                            try {
                                StopAndWait(control); ChangeCommand(scm, previousCommand);
                                control.Start(); control.WaitForStatus(ServiceControllerStatus.Running, TimeSpan.FromSeconds(20));
                            } catch { throw new Rejected("upgrade_recovery_pending", "Upgrade failed and the previous service could not be restarted; protected recovery records are preserved."); }
                            throw new Rejected("upgrade_failed", "The new service did not start; its previous configuration was restored.");
                        }
                        throw;
                    }
                }
            } finally { CloseServiceHandle(scm); }
        }
    }
}
