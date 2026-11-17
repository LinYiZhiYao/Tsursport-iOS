using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
using Microsoft.Win32;

namespace TsursportLauncher
{
    /// <summary>
    /// Tsursport iOS 启动器（原生 exe，约 15 KB）
    ///
    /// 作用：找到本机 Python，用 pythonw 拉起同目录的 tsursport_gui.py。
    /// 特点：不自解压、不写用户目录、不调用安装服务 —— 绕开所有权限限制。
    /// </summary>
    static class Program
    {
        const string AppName = "Tsursport iOS";

        [STAThread]
        static void Main(string[] args)
        {
            var baseDir = AppDomain.CurrentDomain.BaseDirectory;
            var script = Path.Combine(baseDir, "tsursport_gui.py");

            if (!File.Exists(script))
            {
                Error("缺少 tsursport_gui.py。\n\n请把本 exe 与 tsursport_gui.py、web 文件夹放在同一目录。\n当前目录：\n" + baseDir);
                return;
            }

            var python = FindPython();
            if (python == null)
            {
                Error("没有找到 Python。\n\n请先安装 Python 3.9+（安装时勾选 Add Python to PATH），\n" +
                      "或在本目录运行 start_gui.bat。");
                return;
            }

            var psi = new ProcessStartInfo
            {
                FileName = python,
                Arguments = Quote(script) + " " + string.Join(" ", args),
                WorkingDirectory = baseDir,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            try
            {
                Process.Start(psi);
            }
            catch (Exception e)
            {
                Error("启动失败：" + e.Message + "\n\n可以改为直接运行本目录的 start_gui.bat。");
            }
        }

        static string Quote(string s) { return "\"" + s + "\""; }

        static string FindPython()
        {
            // 1) PATH 里的 pythonw
            var fromPath = Which("pythonw.exe");
            if (fromPath != null) return fromPath;
            fromPath = Which("python.exe");
            if (fromPath != null) return fromPath;

            // 2) 注册表里登记的安装路径
            string[] keys = {
                @"SOFTWARE\Python\PythonCore\3.14\InstallPath",
                @"SOFTWARE\Python\PythonCore\3.13\InstallPath",
                @"SOFTWARE\Python\PythonCore\3.12\InstallPath",
                @"SOFTWARE\Python\PythonCore\3.11\InstallPath",
                @"SOFTWARE\Python\PythonCore\3.10\InstallPath",
                @"SOFTWARE\Python\PythonCore\3.9\InstallPath"
            };
            foreach (var k in keys)
            {
                foreach (var root in new[] { Registry.CurrentUser, Registry.LocalMachine })
                {
                    try
                    {
                        using (var key = root.OpenSubKey(k))
                        {
                            if (key == null) continue;
                            var dir = key.GetValue(null) as string;
                            if (string.IsNullOrEmpty(dir)) continue;
                            var w = Path.Combine(dir, "pythonw.exe");
                            if (File.Exists(w)) return w;
                            var p = Path.Combine(dir, "python.exe");
                            if (File.Exists(p)) return p;
                        }
                    }
                    catch { }
                }
            }

            // 3) 常见安装位置
            var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            foreach (var v in new[] { "Python314", "Python313", "Python312", "Python311", "Python310", "Python39" })
            {
                var p = Path.Combine(local, "Programs", "Python", v, "pythonw.exe");
                if (File.Exists(p)) return p;
            }
            return null;
        }

        static string Which(string exe)
        {
            try
            {
                var path = Environment.GetEnvironmentVariable("PATH") ?? "";
                foreach (var dir in path.Split(';'))
                {
                    if (string.IsNullOrWhiteSpace(dir)) continue;
                    try
                    {
                        var full = Path.Combine(dir.Trim(), exe);
                        if (File.Exists(full)) return full;
                    }
                    catch { }
                }
            }
            catch { }
            return null;
        }

        static void Error(string msg)
        {
            MessageBox.Show(msg, AppName, MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }
}
