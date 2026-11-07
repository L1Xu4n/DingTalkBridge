# One structure-only CEF accessibility probe

This experiment leaves production collection frozen. It obtains the latest verified CEF source from `--mode regions`, verifies Default desktop, image path/PID/window classes, and queries only MSAA Role, State and childCount. Neither Name, Value nor Description is read. No image is captured and no message is stored.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python experimental/accessibility-probe/run.py --config 'D:\Program Files\DingTalkBridge\config.local.json'
```

Run on the interactive Default desktop. It compiles the standalone .NET Framework `Probe.cs`, scopes an OUTOFCONTEXT WinEvent listener to the selected process, pumps messages for 12 seconds and unhooks. Window queries are bounded; the Python supervisor times out the experiment at 40 seconds. The compiler timeout is 30 seconds and geometry timeout 20 seconds.

The report contains only structural statistics. For both the CEF source and its exact `Chrome_WidgetWin_1` native descendant it compares `AccessibleObjectFromWindow(OBJID_CLIENT)` with standard `WM_GETOBJECT(OBJID_CLIENT)` plus `ObjectFromLresult`. It does not change screen-reader flags or settings, inject code, restart DingTalk, or call an unknown ABI.

Actual result in `report.json`:

- CEF: 11 nodes, root role 10 (Client); standard WM_GETOBJECT returned zero, so there was no native object result to unmarshal.
- Chrome child: 3 nodes, role histogram `{16: 2, 10: 1}` (two Panes and one Client), identical through both supported retrieval paths.
- Before hook, at 2/6/10 seconds, and after hook: unchanged. No Document (15) or StaticText (41) roles appeared; provider errors were zero and no traversal was truncated.
- Hook registration and unhook succeeded; event notifications were zero. Experiment exit code was zero. Subsequent process inventory found zero DingTalkUia/MsaaStructureProbe/SafeWindowCrop helpers.

This did not expose a semantic chat-body accessibility tree. The experiment stopped; it does not justify enabling a production text fallback or making a claim about inaccessible message contents.

Sources: [SetWinEventHook](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook), [WM_GETOBJECT](https://learn.microsoft.com/en-us/windows/win32/winauto/how-to-handle-wm-getobject).
