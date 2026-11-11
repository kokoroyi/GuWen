using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace GuWenTray
{
    // Windows tray launcher for the source edition.
    //
    // It starts the local FastAPI service with the project's .venv Python and
    // opens http://127.0.0.1:8770/.  It does NOT start any local LLM runtime;
    // AI punctuation is configured in the web page and called over HTTP.
    internal sealed class TrayApp : ApplicationContext
    {
        private const string Url = "http://127.0.0.1:8770/";
        private readonly string _root;
        private readonly string _logPath;
        private readonly NotifyIcon _notify;
        private readonly System.Windows.Forms.Timer _timer;
        private Process _server;
        private bool _startedByUs;
        private bool _opened;
        private bool _exiting;
        private bool _errorShown;
        private DateTime _startedAt;
        private readonly Mutex _mutex;
        private readonly EventWaitHandle _exitEvent;

        public TrayApp(Mutex mutex)
        {
            _mutex = mutex;
            try
            {
                _exitEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\GuWenTrayExit");
            }
            catch
            {
                _exitEvent = null;
            }
            _root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            _logPath = Path.Combine(_root, "logs", "server.log");

            _notify = new NotifyIcon();
            _notify.Icon = LoadAppIcon();
            _notify.Text = "GuWen 古籍整理工作台";
            _notify.Visible = true;
            _notify.DoubleClick += delegate { OpenBrowser(); };

            ContextMenuStrip menu = new ContextMenuStrip();
            ToolStripMenuItem open = new ToolStripMenuItem("打开工作台");
            open.Click += delegate { OpenBrowser(); };
            ToolStripMenuItem data = new ToolStripMenuItem("打开数据目录");
            data.Click += delegate { OpenFolder(Path.Combine(_root, "app", "data")); };
            ToolStripMenuItem logs = new ToolStripMenuItem("打开日志");
            logs.Click += delegate { OpenLogs(); };
            ToolStripMenuItem setup = new ToolStripMenuItem("安装依赖（首次使用）");
            setup.Click += delegate { RunSetup(); };
            ToolStripMenuItem exit = new ToolStripMenuItem("退出 GuWen");
            exit.Click += delegate { ExitApp(); };
            menu.Items.Add(open);
            menu.Items.Add(data);
            menu.Items.Add(logs);
            menu.Items.Add(setup);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(exit);
            _notify.ContextMenuStrip = menu;

            _timer = new System.Windows.Forms.Timer();
            _timer.Interval = 500;
            _timer.Tick += delegate { Tick(); };
            _timer.Start();
            BeginStart();
        }

        private Icon LoadAppIcon()
        {
            try
            {
                Icon icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
                if (icon != null) return icon;
            }
            catch { }
            return SystemIcons.Application;
        }

        private string FindPython()
        {
            string venv = Path.Combine(_root, @".venv\Scripts\python.exe");
            if (File.Exists(venv)) return venv;
            // Compatibility with an older portable package if it happens to be
            // copied beside the launcher.  The clean repo does not contain it.
            string legacy = Path.Combine(_root, @"runtime\python\python.exe");
            if (File.Exists(legacy)) return legacy;
            return null;
        }

        private void BeginStart()
        {
            if (IsHealthy())
            {
                // Already running: only open the page and do not own the process.
                _notify.ShowBalloonTip(2500, "GuWen 已启动", "正在打开工作台…", ToolTipIcon.Info);
                OpenBrowser();
                _opened = true;
                return;
            }

            string python = FindPython();
            if (python == null)
            {
                _notify.ShowBalloonTip(5000, "GuWen 需要安装依赖",
                    "未找到 .venv。请先运行 setup.bat，或右键托盘图标选择“安装依赖”。",
                    ToolTipIcon.Warning);
                MessageBox.Show(
                    "未找到 Python 运行环境。\n\n" +
                    "首次使用请先双击 setup.bat 安装依赖，\n" +
                    "或右键托盘图标选择“安装依赖（首次使用）”。",
                    "GuWen 首次使用", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            string server = Path.Combine(_root, "app", "server.py");
            if (!File.Exists(server))
            {
                ShowError("缺少 app/server.py，请确认压缩包已完整解压。");
                return;
            }

            Directory.CreateDirectory(Path.Combine(_root, "logs"));
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = python;
            psi.Arguments = "\"" + server + "\"";
            psi.WorkingDirectory = _root;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            _server = new Process();
            _server.StartInfo = psi;
            _server.OutputDataReceived += delegate(object s, DataReceivedEventArgs e) { AppendLog(e.Data); };
            _server.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { AppendLog(e.Data); };
            try
            {
                _server.Start();
                _server.BeginOutputReadLine();
                _server.BeginErrorReadLine();
                _startedByUs = true;
                _startedAt = DateTime.UtcNow;
                _notify.ShowBalloonTip(1800, "GuWen 正在启动", "正在加载本地服务…", ToolTipIcon.Info);
            }
            catch (Exception ex)
            {
                ShowError("启动失败：" + ex.Message);
            }
        }

        private void Tick()
        {
            if (_exiting) return;
            if (_exitEvent != null && _exitEvent.WaitOne(0))
            {
                ExitApp();
                return;
            }
            if (_opened) return;
            if (IsHealthy())
            {
                _opened = true;
                OpenBrowser();
                _notify.ShowBalloonTip(2200, "GuWen 已启动", "工作台已在浏览器打开。", ToolTipIcon.Info);
                return;
            }
            if (_server != null && _server.HasExited)
            {
                if (!_errorShown)
                {
                    _errorShown = true;
                    ShowError("本地服务已退出（exit=" + _server.ExitCode + "），请打开日志排查。\n" + _logPath);
                }
                return;
            }
            if ((DateTime.UtcNow - _startedAt).TotalSeconds > 45)
            {
                if (!_errorShown)
                {
                    _errorShown = true;
                    ShowError("本地服务启动超时，请打开日志排查。\n" + _logPath);
                }
            }
        }

        private bool IsHealthy()
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(Url + "api/health");
                req.Proxy = null;
                req.Timeout = 800;
                req.ReadWriteTimeout = 800;
                req.KeepAlive = false;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    return (int)resp.StatusCode == 200;
                }
            }
            catch { return false; }
        }

        private void OpenBrowser()
        {
            try
            {
                Process.Start(new ProcessStartInfo(Url) { UseShellExecute = true });
            }
            catch { }
        }

        private void OpenFolder(string path)
        {
            try
            {
                Directory.CreateDirectory(path);
                Process.Start(new ProcessStartInfo("explorer.exe", "\"" + path + "\"") { UseShellExecute = true });
            }
            catch { }
        }

        private void OpenLogs()
        {
            try
            {
                Directory.CreateDirectory(Path.Combine(_root, "logs"));
                Process.Start(new ProcessStartInfo("notepad.exe", "\"" + _logPath + "\"") { UseShellExecute = true });
            }
            catch { OpenFolder(Path.Combine(_root, "logs")); }
        }

        private void RunSetup()
        {
            string setup = Path.Combine(_root, "setup.bat");
            if (!File.Exists(setup))
            {
                ShowError("缺少 setup.bat。\n请手动安装 requirements.txt 中的依赖。");
                return;
            }
            try
            {
                Process.Start(new ProcessStartInfo(setup) { WorkingDirectory = _root, UseShellExecute = true });
            }
            catch (Exception ex)
            {
                ShowError("无法启动 setup.bat：" + ex.Message);
            }
        }

        private void ShowError(string message)
        {
            try
            {
                _notify.BalloonTipTitle = "GuWen 启动失败";
                _notify.BalloonTipText = message;
                _notify.BalloonTipIcon = ToolTipIcon.Error;
                _notify.ShowBalloonTip(6000);
            }
            catch { }
        }

        private void AppendLog(string line)
        {
            if (String.IsNullOrEmpty(line)) return;
            try
            {
                string dir = Path.GetDirectoryName(_logPath);
                if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
                File.AppendAllText(_logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss ") + line + Environment.NewLine, Encoding.UTF8);
            }
            catch { }
        }

        private void ExitApp()
        {
            if (_exiting) return;
            _exiting = true;
            _timer.Stop();
            try { _notify.Visible = false; } catch { }
            if (_startedByUs && _server != null && !_server.HasExited)
            {
                try
                {
                    ProcessStartInfo psi = new ProcessStartInfo("taskkill", "/PID " + _server.Id + " /T /F");
                    psi.UseShellExecute = false;
                    psi.CreateNoWindow = true;
                    using (Process killer = Process.Start(psi))
                    {
                        if (killer != null) killer.WaitForExit(4000);
                    }
                    _server.WaitForExit(4000);
                }
                catch { }
            }
            try { _notify.Dispose(); } catch { }
            try { if (_mutex != null) _mutex.ReleaseMutex(); } catch { }
            ExitThread();
        }
    }

    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            if (args != null && args.Length > 0 && String.Equals(args[0], "--exit", StringComparison.OrdinalIgnoreCase))
            {
                try
                {
                    EventWaitHandle ev = EventWaitHandle.OpenExisting(@"Local\GuWenTrayExit");
                    ev.Set();
                    ev.Dispose();
                }
                catch { }
                return;
            }
            bool created;
            Mutex mutex = new Mutex(true, @"Local\GuWenTrayApp", out created);
            if (!created)
            {
                try { Process.Start(new ProcessStartInfo("http://127.0.0.1:8770/") { UseShellExecute = true }); }
                catch { }
                return;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new TrayApp(mutex));
        }
    }
}
