Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class Frz {
  [DllImport("ntdll.dll")] public static extern int NtSuspendProcess(IntPtr h);
  [DllImport("ntdll.dll")] public static extern int NtResumeProcess(IntPtr h);
  [DllImport("psapi.dll")] public static extern bool EmptyWorkingSet(IntPtr h);
}
"@
function WS($p) { $p.Refresh(); [math]::Round($p.WorkingSet64/1MB,1) }

# victim: child powershell that allocates + touches 800 MB, then idles
$victim = Start-Process powershell -PassThru -WindowStyle Hidden -ArgumentList '-NoProfile','-Command',
  '$a = New-Object byte[] (800MB); for($i=0;$i -lt $a.Length;$i+=4096){$a[$i]=1}; while($true){Start-Sleep 1}'
Start-Sleep 8
"baseline WS MB      : $(WS $victim)"

$t = Measure-Command { [Frz]::NtSuspendProcess($victim.Handle) | Out-Null; [Frz]::EmptyWorkingSet($victim.Handle) | Out-Null }
Start-Sleep 2
"after freeze WS MB  : $(WS $victim)   (freeze took $([int]$t.TotalMilliseconds) ms)"
"free RAM MB         : $([math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1KB))"

$t = Measure-Command { [Frz]::NtResumeProcess($victim.Handle) | Out-Null }
Start-Sleep 3
"after resume WS MB  : $(WS $victim)   (resume call $([int]$t.TotalMilliseconds) ms; pages fault back lazily)"
"alive               : $(-not $victim.HasExited)"
Stop-Process $victim -Force
