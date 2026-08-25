using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public sealed class CnncScheduledProcessResult
{
    public int ExitCode { get; private set; }
    public bool TimedOut { get; private set; }
    public bool WriterFailed { get; private set; }
    public string Diagnostic { get; private set; }

    public CnncScheduledProcessResult(
        int exitCode,
        bool timedOut,
        bool writerFailed,
        string diagnostic)
    {
        ExitCode = exitCode;
        TimedOut = timedOut;
        WriterFailed = writerFailed;
        Diagnostic = diagnostic ?? String.Empty;
    }
}

public static class CnncScheduledProcessHost
{
    private const uint JobObjectExtendedLimitInformation = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;
    private const int TimeoutExitCode = 124;
    private const int WriterFailureExitCode = 74;
    private const int StartFailureExitCode = 70;

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public IntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        uint informationClass,
        IntPtr information,
        uint informationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    private static IntPtr CreateKillOnCloseJob()
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
        }

        ExtendedLimitInformation limits = new ExtendedLimitInformation();
        limits.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
        int size = Marshal.SizeOf(typeof(ExtendedLimitInformation));
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(limits, buffer, false);
            if (!SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                buffer,
                (uint)size))
            {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "SetInformationJobObject failed");
            }
        }
        catch
        {
            CloseHandle(job);
            throw;
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
        return job;
    }

    private static string QuoteArgument(string argument)
    {
        if (argument == null)
        {
            return "\"\"";
        }
        if (argument.Length > 0 &&
            argument.IndexOfAny(new char[] { ' ', '\t', '\n', '\v', '\"' }) < 0)
        {
            return argument;
        }

        StringBuilder quoted = new StringBuilder();
        quoted.Append('\"');
        int backslashes = 0;
        foreach (char value in argument)
        {
            if (value == '\\')
            {
                backslashes++;
                continue;
            }
            if (value == '\"')
            {
                quoted.Append('\\', (backslashes * 2) + 1);
                quoted.Append('\"');
                backslashes = 0;
                continue;
            }
            quoted.Append('\\', backslashes);
            backslashes = 0;
            quoted.Append(value);
        }
        quoted.Append('\\', backslashes * 2);
        quoted.Append('\"');
        return quoted.ToString();
    }

    private static string JoinArguments(string[] arguments)
    {
        if (arguments == null || arguments.Length == 0)
        {
            return String.Empty;
        }
        string[] quoted = new string[arguments.Length];
        for (int index = 0; index < arguments.Length; index++)
        {
            quoted[index] = QuoteArgument(arguments[index]);
        }
        return String.Join(" ", quoted);
    }

    public static CnncScheduledProcessResult Run(
        string executable,
        string[] arguments,
        string workingDirectory,
        TextWriter writer,
        int timeoutMilliseconds)
    {
        if (String.IsNullOrWhiteSpace(executable))
        {
            throw new ArgumentException("executable is required", "executable");
        }
        if (writer == null)
        {
            throw new ArgumentNullException("writer");
        }
        if (timeoutMilliseconds <= 0)
        {
            throw new ArgumentOutOfRangeException("timeoutMilliseconds");
        }

        IntPtr job = IntPtr.Zero;
        Process process = null;
        object outputLock = new object();
        Exception writerFailure = null;
        ManualResetEvent outputClosed = new ManualResetEvent(false);
        ManualResetEvent errorClosed = new ManualResetEvent(false);
        Stopwatch stopwatch = Stopwatch.StartNew();
        try
        {
            job = CreateKillOnCloseJob();
            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = executable;
            startInfo.Arguments = JoinArguments(arguments);
            startInfo.WorkingDirectory = workingDirectory;
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.RedirectStandardOutput = true;
            startInfo.RedirectStandardError = true;
            startInfo.StandardOutputEncoding = new UTF8Encoding(false);
            startInfo.StandardErrorEncoding = new UTF8Encoding(false);

            process = new Process();
            process.StartInfo = startInfo;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
            {
                if (eventArgs.Data == null)
                {
                    outputClosed.Set();
                    return;
                }
                lock (outputLock)
                {
                    if (writerFailure != null)
                    {
                        return;
                    }
                    try
                    {
                        writer.WriteLine(eventArgs.Data);
                    }
                    catch (Exception exception)
                    {
                        writerFailure = exception;
                    }
                }
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
            {
                if (eventArgs.Data == null)
                {
                    errorClosed.Set();
                    return;
                }
                lock (outputLock)
                {
                    if (writerFailure != null)
                    {
                        return;
                    }
                    try
                    {
                        writer.WriteLine(eventArgs.Data);
                    }
                    catch (Exception exception)
                    {
                        writerFailure = exception;
                    }
                }
            };

            try
            {
                if (!process.Start())
                {
                    return new CnncScheduledProcessResult(
                        StartFailureExitCode,
                        false,
                        false,
                        "process start returned false");
                }
            }
            catch (Exception exception)
            {
                return new CnncScheduledProcessResult(
                    StartFailureExitCode,
                    false,
                    false,
                    exception.GetType().Name);
            }

            if (!AssignProcessToJobObject(job, process.Handle))
            {
                int error = Marshal.GetLastWin32Error();
                try
                {
                    process.Kill();
                    process.WaitForExit(5000);
                }
                catch
                {
                }
                return new CnncScheduledProcessResult(
                    StartFailureExitCode,
                    false,
                    false,
                    "AssignProcessToJobObject failed: " + error);
            }

            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            bool timedOut = false;
            bool outputFailed = false;
            while (!process.WaitForExit(50))
            {
                lock (outputLock)
                {
                    outputFailed = writerFailure != null;
                }
                if (outputFailed || stopwatch.ElapsedMilliseconds >= timeoutMilliseconds)
                {
                    timedOut = !outputFailed;
                    TerminateJobObject(
                        job,
                        outputFailed ? (uint)WriterFailureExitCode : (uint)TimeoutExitCode);
                    break;
                }
            }

            if (!process.HasExited)
            {
                process.WaitForExit(5000);
            }
            int childExitCode = process.HasExited ? process.ExitCode : StartFailureExitCode;
            TerminateJobObject(job, (uint)Math.Max(0, childExitCode));
            if (!process.HasExited)
            {
                try
                {
                    process.Kill();
                }
                catch
                {
                }
            }
            if (!process.WaitForExit(5000))
            {
                return new CnncScheduledProcessResult(
                    StartFailureExitCode,
                    false,
                    false,
                    "terminated child did not exit within cleanup deadline");
            }
            Stopwatch drainStopwatch = Stopwatch.StartNew();
            bool outputDrainCompleted = outputClosed.WaitOne(5000);
            int errorDrainWait = Math.Max(
                0,
                5000 - (int)drainStopwatch.ElapsedMilliseconds);
            bool drainsCompleted = outputDrainCompleted && errorClosed.WaitOne(errorDrainWait);

            lock (outputLock)
            {
                outputFailed = writerFailure != null;
            }
            if (outputFailed)
            {
                return new CnncScheduledProcessResult(
                    WriterFailureExitCode,
                    false,
                    true,
                    writerFailure.GetType().Name);
            }
            if (!drainsCompleted)
            {
                return new CnncScheduledProcessResult(
                    WriterFailureExitCode,
                    false,
                    true,
                    "OutputDrainTimeout");
            }
            if (timedOut)
            {
                return new CnncScheduledProcessResult(
                    TimeoutExitCode,
                    true,
                    false,
                    "child deadline exceeded");
            }
            return new CnncScheduledProcessResult(childExitCode, false, false, String.Empty);
        }
        finally
        {
            if (process != null)
            {
                process.Dispose();
            }
            outputClosed.Dispose();
            errorClosed.Dispose();
            if (job != IntPtr.Zero)
            {
                CloseHandle(job);
            }
        }
    }
}
