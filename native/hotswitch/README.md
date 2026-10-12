# Restricted game-input restart helper

The 2.4.0 bridge enables automatic input switching only for validated APEX
USB / 2.4G receiver devices (`VID_37D7&PID_2502`). The Bluetooth operation
described below is a lower-level helper capability, not an automatic switching
path enabled by this release. Bluetooth keeps the existing bridge-first flow.

Build with `python tools/build_hotswitch_helper.py`. Building compiles and runs
pure request/topology tests plus an isolated local-pipe status smoke test.
No service is installed and no device is reset.
The generated `bin/` directory is a build artifact, not source.
`DSBridgeHotSwitch.exe --validate-device EXACT_HID_INSTANCE_ID` performs only
present-tree validation and prints the independently selected `root_id`.
It never installs the service or calls the device restart API.
`--validate-usb-port EXACT_HID_INSTANCE_ID` additionally performs read-only USB
hub/port/driver-key/descriptor/serial queries and reports the derived physical
root, parent hub and port. It never sends a cycle request or writes a journal.

An administrator explicitly installs `DSBridgeHotSwitch.exe --install --owner-sid
<interactive-user-SID>`. The installer never bypasses UAC. It copies this version
to `Program Files/DSFeedbackBridge/HotSwitch/1.0.6/DSBridgeHotSwitch.exe`, validates
protected ACLs and rejects reparse points and different contents at that version.
It creates the automatic LocalSystem service `DSBridgeHotSwitch`. The owner SID
is in its SCM-protected command line. No scheduled task or user-writable script
is used. Installing a conflicting existing service fails closed. Version 1.0.6
can migrate the exact reviewed 1.0.0 through 1.0.5 binaries after checking their hashes,
protected paths, same user SID, LocalSystem identity and automatic startup.
It waits for Stop before changing SCM's executable path. A failed new start
attempts to restore the prior configuration, including if changing SCM's command
fails after stopping the old service. Old binaries are never deleted. SCM failure
actions restart this service after 5/15/60 seconds; they never run commands or
reboot Windows.

`--pipe-self-test` runs 12 real local status exchanges through a fresh random
pipe under the current account with identification-only client tokens. Its
endpoint refuses all device operations; it never installs or contacts the real
service. This separately tests .NET Framework asynchronous completion/cleanup.

Local named pipe: `DSBridgeHotSwitch.v1`. One UTF-8 JSON line per connection,
at most 2048 bytes, with 5-second read/write deadlines and one serial request at
a time. Remote clients are rejected. Only SYSTEM and the installation user have
pipe access. The user's concrete access mask is `0x0012019B` (no pipe-instance
creation permission); clients should use that mask rather than generic write.
The server reads TokenUser using identification impersonation and returns to
LocalSystem before any PnP operation. Clients must independently verify the
server PID matches the running SCM service, to reject pipe impersonation.

Requests: `{"version":1,"operation":"status"}` or
`{"version":1,"operation":"restart","device_id":"HID\\...\\..."}`.
Unknown fields/operations are rejected. Responses include `ok`, `code`,
`message`, `api_version:1`, `version:"1.0.6"`, `restart_required`; status also
includes `permitted_owner`, `pipe`, `recovery_pending`, `recovery_restart_required`
and optional `recovery_error`;
restart includes the validated `root_id`, `method`, CM result codes and observed
devnode `status`/`problem`. A ready pipe does not mean a pending transaction has
finished: a new bridge startup must reject `recovery_pending:true`.
`status_before`, `problem_before` and `pre_needs_restart` record the state before
the transaction. A pre-existing DN_NEED_RESTART bit permits one attempt only if
the exact root is started and lacks DN_HAS_PROBLEM. A raw problem number is
interpreted only when DN_HAS_PROBLEM is set. No code clears a state flag. Any
CM return requesting restart, or any post-state restart bit/problem, remains a
failure; success requires Windows itself to report a healthy post-state.
The helper does not infer that a game adopted the resulting virtual controller.

Device roots are never supplied by the client. The helper independently reads
the present PnP tree, requires physical HID gamepad/joystick usage (hardware or
compatible IDs), excludes keyboard/mouse and known virtual ancestors, and accepts
only the exclusive APEX `HID IG -> USB IG -> USB MI_00` branch or an exclusive
classic Bluetooth `1124` HID profile. For APEX USB it derives the physical
`VID_37D7&PID_2502` composite controller and its direct hub parent. It cross-checks
CM_DRP_ADDRESS, the composite driver key, the port's USB device descriptor and
serial and the complete PnP ancestry. The exact physical instance's driver key
must equal the device-specific key returned for this selected port; this binds
one port without querying any unrelated hub ports. Failed selected-port queries
or ambiguous PnP/interface enumeration refuse the operation.
The exact hub interface is opened, the binding is checked again, and one
`IOCTL_USB_HUB_CYCLE_PORT` selects that controller's one-based port. No hub-reset
IOCTL is used. All interfaces of this one controller briefly disconnect; other
ports and the parent hub are not reset. Windows 8 and later support this
administrator operation. Both Win32 success and the returned USBD status/size/
port must indicate success. After the call, a 15-second monotonic polling window
checks the original selected HID tree, port identity and healthy devnode state.
Synchronous kernel calls themselves are not forcibly terminated on timeout.
Replies retain the input MI_00 `root_id` and additionally include
`physical_root_id`, `hub_id`, `port_index`, `method:"usb_hub_cycle_port"`,
`cycle_attempted`, `cycle_win32_error`, `cycle_usb_status` and `cycle_bytes`.
USB re-enumeration can change the IG/HID instance IDs. Recovery loads the exact
saved MI_00 root and requires its unchanged fingerprint, ancestors and physical
port binding, then independently validates the complete exclusive game-input
subtree again. Only those IG/HID descendants can change. HID and IG must both
be started without problem or restart flags. Success returns `new_input_id`
and `new_input_parent_id` for the current validated chain. If a caller supplies
an absent old HID ID, an alias is permitted only when it exactly matches the
last protected schema-2 journal and the full original root/port binding still
matches; this also works after background recovery completed that record.
There is no machine-wide VID/PID fallback. All other absent IDs fail closed.

Bluetooth still uses `CM_Disable_DevNode(root, 5)` followed by an unconditional
enable compensation for that exclusive profile. WinSDK defines this as
`CM_DISABLE_ABSOLUTE | CM_DISABLE_UI_NOT_OK`; HARDWARE and PERSIST are never used.
The Bluetooth radio and unrelated device roots are never mutated. USB does not
fall back to this method if port validation fails. Each method still requires
real-device testing with existing game handles; pure tests cannot prove that.
The former `DICS_PROPCHANGE` code remains unrouted diagnosis/reference code. It
records before/after flags, and still treats any post-call DI_NEEDRESTART/REBOOT
as failure rather than ignoring pre-existing bits. It is never run first.

Before the device operation, the service flushes an armed record to its fixed protected
`Program Files/DSFeedbackBridge/HotSwitch/State/recovery.json` location. The
record contains only service-validated root/ancestor/descendant identity hashes.
USB schema 2 adds the physical controller, hub, port, driver key, descriptor and
serial binding. Old schema 1 journals remain readable with their CM recovery
semantics. Schema 2 recovery only observes return of the original complete input
and port identity. It never repeats the cycle and never calls CM enable. Even
when the service crashed before or during the cycle, recovery cannot issue a
second cycle. An absent/replaced controller remains pending. A failed cycle
may finish recovery if the original input is healthy, but the request still
reports failure. Clients must not automatically resend a timed-out restart.
Missing descendants are allowed during recovery; new descendants or changed
root/ancestor identity are rejected. Pipe callers cannot supply recovery roots,
paths or commands. Every disable return/exception attempts to enable the same
root; client disconnect does not cancel that compensation. Only successful
enable plus started/no-problem/no-restart observation can write a completed record.
No record is deleted. An absent root stays pending until the same device returns.

At service startup and approximately every five seconds while idle, an armed
USB record triggers observation; a legacy/BT record triggers enable recovery
when needed, never another disable. Unresolved records
block all new restart transactions. Service stop waits for the accepted
transaction; it never kills a thread stuck in a synchronous device API. A service
crash is handled by SCM restart plus the protected journal. The nonpersistent
disable mode also allows Windows to enable the device after a system restart.
If the original root is already started without DN_HAS_PROBLEM, background
recovery only observes it; it does not repeatedly enable a node waiting for a
system restart. The armed record and explicit restart-needed status remain until
Windows itself reports a healthy state. An accepted restart's finally path still
performs its one enable compensation regardless of the currently observed state.

Primary API references:
- https://learn.microsoft.com/windows-hardware/drivers/install/dif-propertychange
- https://learn.microsoft.com/windows/win32/api/setupapi/nf-setupapi-setupdicallclassinstaller
- https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-createnamedpipea
- https://learn.microsoft.com/windows/win32/api/cfgmgr32/nf-cfgmgr32-cm_disable_devnode
- https://learn.microsoft.com/windows/win32/api/cfgmgr32/nf-cfgmgr32-cm_enable_devnode
- https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/cfgmgr32.h
- https://learn.microsoft.com/windows-hardware/drivers/ddi/usbioctl/ni-usbioctl-ioctl_usb_hub_cycle_port
- https://learn.microsoft.com/windows-hardware/drivers/ddi/usbioctl/ns-usbioctl-_usb_cycle_port_params
- https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/shared/usbioctl.h

The pure self-test does not establish real transport restart behavior. Native
tests must separately verify controller reacquisition, existing-client isolation,
input restoration and preservation of audio and unrelated devices.
