import subprocess
import os
import sys

def browse_script_file():
    """
    Opens native Windows File Dialog to select a single Python (.py) strategy script.
    Thread-safe and works seamlessly from Flask routes or desktop app.
    """
    try:
        initial_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        strat_dir = os.path.join(initial_dir, "Strategy_Files")
        if not os.path.exists(strat_dir):
            strat_dir = initial_dir

        ps_script = f"""
[System.Reflection.Assembly]::LoadWithPartialName("System.windows.forms") | Out-Null
$f = New-Object System.Windows.Forms.OpenFileDialog
$f.Title = "Select Strategy Script (.py)"
$f.InitialDirectory = "{strat_dir.replace('\\', '\\\\')}"
$f.Filter = "Python Files (*.py)|*.py|All Files (*.*)|*.*"
$f.Multiselect = $false
$topForm = New-Object System.Windows.Forms.Form
$topForm.TopMost = $true
$res = $f.ShowDialog($topForm)
if ($res -eq [System.Windows.Forms.DialogResult]::OK) {{
    Write-Output $f.FileName
}}
"""
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=120
        )
        out = proc.stdout.strip()
        if out and os.path.exists(out):
            return out
        return None
    except Exception as e:
        print(f"[browse_script_file error] {e}")
        return None


def browse_data_files():
    """
    Opens native Windows File Dialog to select one or multiple (.parquet, .csv) data files.
    """
    try:
        initial_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir = os.path.join(initial_dir, "Raw_Market_Data")
        if not os.path.exists(data_dir):
            data_dir = initial_dir

        ps_script = f"""
[System.Reflection.Assembly]::LoadWithPartialName("System.windows.forms") | Out-Null
$f = New-Object System.Windows.Forms.OpenFileDialog
$f.Title = "Select Market Data Files (.parquet, .csv)"
$f.InitialDirectory = "{data_dir.replace('\\', '\\\\')}"
$f.Filter = "Data Files (*.parquet;*.csv)|*.parquet;*.csv|Parquet Files (*.parquet)|*.parquet|CSV Files (*.csv)|*.csv|All Files (*.*)|*.*"
$f.Multiselect = $true
$topForm = New-Object System.Windows.Forms.Form
$topForm.TopMost = $true
$res = $f.ShowDialog($topForm)
if ($res -eq [System.Windows.Forms.DialogResult]::OK) {{
    foreach ($file in $f.FileNames) {{
        Write-Output $file
    }}
}}
"""
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=120
        )
        out = proc.stdout.strip()
        if out:
            files = [line.strip() for line in out.splitlines() if line.strip() and os.path.exists(line.strip())]
            return files
        return []
    except Exception as e:
        print(f"[browse_data_files error] {e}")
        return []


if __name__ == "__main__":
    print("Testing browse_script_file()...")
    f = browse_script_file()
    print("Selected:", f)
