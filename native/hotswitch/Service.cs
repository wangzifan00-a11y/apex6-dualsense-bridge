using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace DSBridge.HotSwitch {
    internal sealed class SwitchService : ServiceBase {
        internal const string Name = "DSBridgeHotSwitch";
        internal const string Pipe = "DSBridgeHotSwitch.v1";
        readonly string owner;
        readonly bool diagnostic;
        readonly string pipeName;
        readonly ManualResetEvent stop = new ManualResetEvent(false);
        readonly object gate = new object();
        Thread worker;
        NamedPipeServerStream current;
        string diagnosticError;
        SwitchTransaction transaction;
        DateTime nextRecoveryAttempt = DateTime.MinValue;
        internal SwitchService(string ownerSid, bool diagnosticMode = false) {
            owner = ownerSid; diagnostic = diagnosticMode;
            pipeName = diagnosticMode ? "DSBridgeHotSwitch.SelfTest." + Guid.NewGuid().ToString("N") : Pipe;
            ServiceName = Name; AutoLog = false; CanStop = true;
        }
        protected override void OnStart(string[] args) {
            if (WindowsIdentity.GetCurrent().User.Value != "S-1-5-18") throw new Rejected("service_identity", "Service must run as LocalSystem.");
            transaction = new SwitchTransaction(new DurableJournal(), new Devices.Operations(), new UsbPortOperations());
            stop.Reset(); worker = new Thread(Run) { IsBackground = true, Name = "Bounded game-input requests" }; worker.Start();
        }
        protected override void OnStop() {
            stop.Set(); lock (gate) { if (current != null) current.Dispose(); }
            // Stop/caller cancellation cannot abandon an accepted disable/enable
            // transaction. In-flight synchronous PnP calls are never terminated.
            while (worker != null && !worker.Join(8000)) RequestAdditionalTime(10000);
        }
        [StructLayout(LayoutKind.Sequential)] struct SecurityAttributes { internal uint Length; internal IntPtr Descriptor; internal int Inherit; }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafePipeHandle CreateNamedPipeW(string name, uint mode, uint pipeMode, uint instances, uint outBytes, uint inBytes, uint timeout, ref SecurityAttributes attributes);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool CancelIoEx(SafePipeHandle handle, IntPtr overlapped);
        NamedPipeServerStream OpenPipe() {
            var security = new PipeSecurity(); security.SetAccessRuleProtection(true, false);
            security.SetOwner(new SecurityIdentifier(diagnostic ? owner : "S-1-5-18"));
            security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier("S-1-5-2"), PipeAccessRights.FullControl, AccessControlType.Deny));
            security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier("S-1-5-7"), PipeAccessRights.FullControl, AccessControlType.Deny));
            security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier("S-1-5-18"), PipeAccessRights.FullControl, AccessControlType.Allow));
            security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(owner), (PipeAccessRights)0x0012019B, AccessControlType.Allow));
            byte[] binary = security.GetSecurityDescriptorBinaryForm(); GCHandle pinned = GCHandle.Alloc(binary, GCHandleType.Pinned);
            try {
                var attributes = new SecurityAttributes { Length = (uint)Marshal.SizeOf(typeof(SecurityAttributes)), Descriptor = pinned.AddrOfPinnedObject() };
                // Duplex, overlapped, first instance; byte mode + reject remote clients.
                SafePipeHandle handle = CreateNamedPipeW(@"\\.\pipe\" + pipeName, 3 | 0x40000000 | 0x00080000, 8, 1, 4096, 4096, 5000, ref attributes);
                if (handle.IsInvalid) { int error = Marshal.GetLastWin32Error(); handle.Dispose(); throw new Win32Exception(error); }
                try { return new NamedPipeServerStream(PipeDirection.InOut, true, false, handle); }
                catch { handle.Dispose(); throw; }
            } finally { pinned.Free(); }
        }
        void Await(IAsyncResult pending, NamedPipeServerStream pipe, int milliseconds) {
            int result = WaitHandle.WaitAny(new WaitHandle[] { pending.AsyncWaitHandle, stop }, milliseconds);
            if (result != 0) {
                CancelIoEx(pipe.SafePipeHandle, IntPtr.Zero);
                pipe.Dispose(); // Complete pending overlapped I/O before releasing its event.
                throw new Rejected("timeout", "Pipe request timed out.");
            }
        }
        static void ClosePending(IAsyncResult pending) {
            try {
                WaitHandle handle = pending.AsyncWaitHandle;
                if (handle.WaitOne(0)) { handle.Close(); return; }
                // Cancellation completion can race Dispose. A single-shot wait
                // owns the event only while the Framework still owns live I/O.
                ThreadPool.RegisterWaitForSingleObject(handle, delegate(object value, bool timedOut) {
                    try { ((WaitHandle)value).Close(); } catch (ObjectDisposedException) { }
                }, handle, Timeout.Infinite, true);
            } catch (ObjectDisposedException) {
                // .NET Framework EndWaitForConnection/EndRead/EndWrite can
                // release this event themselves. Cleanup must be idempotent.
            }
        }
        string ReadLine(NamedPipeServerStream pipe) {
            var bytes = new List<byte>(); byte[] chunk = new byte[256]; DateTime deadline = DateTime.UtcNow.AddSeconds(5);
            while (bytes.Count <= Protocol.MaxBytes) {
                int remaining = (int)(deadline - DateTime.UtcNow).TotalMilliseconds;
                if (remaining <= 0) throw new Rejected("timeout", "Pipe request timed out.");
                IAsyncResult read = pipe.BeginRead(chunk, 0, chunk.Length, null, null);
                try { Await(read, pipe, remaining); int count = pipe.EndRead(read);
                    if (count == 0) throw new Rejected("invalid_request", "Request ended before newline.");
                    for (int i = 0; i < count; i++) {
                        if (chunk[i] == 10) {
                            if (i != count - 1 || bytes.Count == 0 || bytes.Count > Protocol.MaxBytes) throw new Rejected("invalid_request", "Exactly one JSON line is required.");
                            return new UTF8Encoding(false, true).GetString(bytes.ToArray());
                        }
                        bytes.Add(chunk[i]); if (bytes.Count > Protocol.MaxBytes) throw new Rejected("invalid_request", "Request exceeds size limit.");
                    }
                } finally { ClosePending(read); }
            }
            throw new Rejected("invalid_request", "Request exceeds size limit.");
        }
        Dictionary<string, object> Execute(Request request) {
            if (request.Operation == "status") {
                var response = Protocol.Reply(true, "ready", "Physical game-input restart helper is ready.");
                response["permitted_owner"] = owner; response["pipe"] = pipeName;
                if (!diagnostic) {
                    response["recovery_pending"] = transaction.Pending();
                    response["recovery_restart_required"] = transaction.LastRecoveryRestartRequired;
                    if (transaction.LastRecoveryError != null) response["recovery_error"] = transaction.LastRecoveryError;
                }
                return response;
            }
            if (diagnostic) throw new Rejected("diagnostic_only", "Pipe self-test does not permit any device operation.");
            return transaction.Restart(request.DeviceId);
        }
        void Run() {
            while (!stop.WaitOne(0)) {
                try {
                    if (!diagnostic && DateTime.UtcNow >= nextRecoveryAttempt) {
                        nextRecoveryAttempt = DateTime.UtcNow.AddSeconds(5);
                        try { transaction.RecoverPending(); }
                        catch (Exception error) { transaction.LastRecoveryError = error.Message; }
                    }
                    using (NamedPipeServerStream pipe = OpenPipe()) {
                        lock (gate) { current = pipe; }
                        IAsyncResult waiting = pipe.BeginWaitForConnection(null, null);
                        try {
                            // A pending unplugged root is retried after return,
                            // even if the GUI that initiated it has disappeared.
                            Await(waiting, pipe, diagnostic ? Timeout.Infinite : 5000);
                            pipe.EndWaitForConnection(waiting);
                        } finally { ClosePending(waiting); }
                        Dictionary<string, object> reply;
                        try {
                            string line = ReadLine(pipe), caller = null;
                            // Identification impersonation is sufficient to inspect TokenUser.
                            // No file, device or privileged work occurs under the client's token.
                            pipe.RunAsClient(delegate { caller = WindowsIdentity.GetCurrent().User.Value; });
                            if (caller != owner) throw new Rejected("wrong_owner", "This service belongs to another Windows user.");
                            reply = Execute(Protocol.Parse(line));
                        }
                        catch (Rejected error) {
                            reply = Protocol.Reply(false, error.Code, error.Message);
                            if (error.Code == "restart_required") reply["restart_required"] = true;
                        }
                        catch (Win32Exception error) { reply = Protocol.Reply(false, "native_error", "Windows rejected the operation (" + error.NativeErrorCode + ")."); }
                        catch { reply = Protocol.Reply(false, "request_failed", "The request could not be safely completed."); }
                        if (!stop.WaitOne(0)) {
                            byte[] data = new UTF8Encoding(false).GetBytes(Protocol.Json().Serialize(reply) + "\n");
                            IAsyncResult write = pipe.BeginWrite(data, 0, data.Length, null, null);
                            try { Await(write, pipe, 5000); pipe.EndWrite(write); }
                            finally { ClosePending(write); }
                        }
                    }
                } catch (Exception error) {
                    if (diagnostic) diagnosticError = error.GetType().Name + ": " + error.Message;
                    if (!stop.WaitOne(1000)) continue;
                }
                finally { lock (gate) { current = null; } }
            }
        }
        internal static Dictionary<string, object> PipeSelfTest() {
            string sid = WindowsIdentity.GetCurrent().User.Value;
            using (var service = new SwitchService(sid, true)) {
                service.worker = new Thread(service.Run) { IsBackground = true }; service.worker.Start();
                try {
                    int passed = 0;
                    for (int cycle = 0; cycle < 12; cycle++) {
                        using (var client = new NamedPipeClientStream(".", service.pipeName,
                            (PipeAccessRights)0x0012019B, PipeOptions.Asynchronous,
                            TokenImpersonationLevel.Identification, HandleInheritability.None)) {
                            client.Connect(4000);
                            string text = "{\"version\":1,\"operation\":\"status\"}\n";
                            byte[] send = Encoding.UTF8.GetBytes(text); client.Write(send, 0, send.Length);
                            byte[] buffer = new byte[4096]; IAsyncResult read = client.BeginRead(buffer, 0, buffer.Length, null, null);
                            int size;
                            try {
                                if (!read.AsyncWaitHandle.WaitOne(5000)) throw new Exception("Pipe smoke timed out: " + service.diagnosticError);
                                size = client.EndRead(read);
                            } finally { ClosePending(read); }
                            if (size == 0) throw new Exception("Pipe smoke disconnected: " + service.diagnosticError);
                            var response = Protocol.Json().DeserializeObject(Encoding.UTF8.GetString(buffer, 0, size).Trim()) as Dictionary<string, object>;
                            if (response == null) throw new Exception("Pipe smoke received no object.");
                            if (!Object.Equals(response["ok"], true) || !Object.Equals(response["version"], Protocol.Version) ||
                                !Object.Equals(response["permitted_owner"], sid)) throw new Exception("Pipe smoke identity/protocol mismatch.");
                            passed++;
                        }
                    }
                    var reply = Protocol.Reply(true, "pipe_self_test_passed", "Local identification-token roundtrips passed; no service installation or device API was used.");
                    reply["roundtrips"] = passed; return reply;
                } finally {
                    service.stop.Set(); lock (service.gate) { if (service.current != null) service.current.Dispose(); }
                    if (!service.worker.Join(8000)) throw new Exception("Pipe self-test worker did not finish.");
                }
            }
        }
    }
}
