import io
import importlib.metadata
import os
import re
import signal
import socket
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta
from functools import wraps

import pandas as pd
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Header, Footer, RichLog, DataTable, Tabs, Tab


ERROR_LOG_PATH = os.path.expanduser("~/.slurman_error.log")
ERROR_LOG_LOCK = threading.Lock()
HOSTNAME = socket.gethostname()

TABLES = ("job", "node", "history")
HISTORY_RANGES = {"1 week": 7, "1 month": 30, "4 months": 120, "1 year": 365}

# stages in which each action is allowed (actions not listed are always allowed)
ACTION_STAGES = {
    "display_nodes": TABLES,
    "display_history_jobs": TABLES,
    "display_jobs": TABLES,
    "select": ("job", "select"),
    "select_inverse": ("job", "select"),
    "refresh": (*TABLES, "job_log"),
    "confirm": ("delete",),
    "sort": TABLES,
    "delete": ("job", "select"),
    "print_gpustat": ("job",),
    "open_shell": ("job",),
    "display_job_log": ("job", "history"),
    "open_with_less": ("job", "job_log", "history"),
    "toggle_user_range": ("job",),
    "toggle_history_range": ("history",),
}


def run_in_thread(func):
    """Decorator to run a function in a separate thread"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()
    return wrapper

def handle_error(func):
    """Decorator to wrap action methods with try-except logic"""
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            error_message = f"[{timestamp}] Error ({func.__name__}): {e}"
            self.info_log.write(error_message)

            trace = traceback.format_exc()
            with ERROR_LOG_LOCK:
                with open(ERROR_LOG_PATH, "a", encoding="utf-8") as error_log:
                    error_log.write(error_message + "\n")
                    error_log.write(trace + "\n")
    return wrapper


class Slurman(App):
    # configuration that can be set from the command line
    verbose = False
    interval = 10
    history_range = "1 week"

    theme = "textual-dark"
    selected_text_style = "bold on orange3"
    border = ("solid", "white")

    CSS_PATH = "slurman.tcss"
    TITLE = f"Slurman (v{importlib.metadata.version('slurman')})"

    BINDINGS = [
        Binding("g", "display_nodes", "GPUs"),
        Binding("h", "display_history_jobs", "History"),
        Binding("j", "display_jobs", "Jobs"),
        Binding("q", "abort", "Abort"),
        Binding("space", "select", "Select"),
        Binding("v", "select_inverse", "Inverse"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "confirm", "Confirm", priority=True, key_display='enter'),
        Binding("s", "sort", "Sort"),
        Binding("d", "delete", "Delete"),
        Binding("G", "print_gpustat", "GPU"),
        Binding("S", "open_shell", "Shell"),
        Binding("l", "display_job_log", "Log"),
        Binding("L", "open_with_less", "Open with less"),
        Binding("J", "toggle_user_range", "State"),
        Binding("H", "toggle_history_range", "Range"),
    ]

    def __init__(self):
        super().__init__()
        # internal states
        self.STAGE = {
            "action": "job",
            "job": {},
            "history": {"sort_column": 0, "sort_ascending": False},
            "node": {},
        }
        self.stats = {}
        self.selected_jobid = []
        self.only_available_nodes = True
        self.only_completed_history = False
        self.only_running_jobs = False
        self.only_my_jobs = True

    def compose(self) -> ComposeResult:
        self.tab_nodes = Tab("GPUs", id="node")
        self.tab_history = Tab("History", id="history")
        self.tab_jobs = Tab("Jobs", id="job")
        self.tab_time = Tab("Time", id="time", disabled=True, classes="time-tab")
        self.tabs = Tabs(self.tab_nodes, self.tab_history, self.tab_jobs, self.tab_time, id="tabs")
        self.tabs.can_focus = False

        self.tables = {name: DataTable(id=f"{name}_table") for name in TABLES}
        self.active_table = "job"

        self.info_log = RichLog(wrap=True, highlight=True, id="info_log", auto_scroll=True)
        self.info_log.can_focus = False
        self.info_log.border_title = "Info"

        self.job_log = RichLog(wrap=True, highlight=True, id="job_log", auto_scroll=False)
        self.job_log.styles.border = self.border
        self.job_log_position = None

        yield Header()
        yield self.tabs
        yield Container(*self.tables.values(), self.info_log, self.job_log)
        yield Footer()

    def on_ready(self) -> None:
        for name in TABLES:
            self.rewrite_table(name)
        self.tabs.active = 'job'
        if self.interval > 0:
            self.set_interval(self.interval, self.auto_refresh)

    def on_tabs_tab_activated(self, message):
        tab_id = message.tab.id
        if self.verbose:
            self.info_log.write(f"Tab activated: {tab_id}")

        self.STAGE["action"] = tab_id
        self.update_table(tab_id)
        self.switch_display(tab_id)
        self.refresh_bindings()

    def check_action(self, action: str, parameters):
        """Check if an action may run."""
        stages = ACTION_STAGES.get(action)
        return stages is None or self.STAGE["action"] in stages

    def _display_tab(self, tab, toggle):
        """Switch to `tab`, or flip its filter via `toggle` if it is already shown."""
        if self.STAGE[self.active_table].get('updating'):
            return
        if self.STAGE["action"] == tab:
            toggle()
            self.rewrite_table(tab, keep_state=True)
            self.switch_display(tab)
        else:
            self.STAGE["action"] = tab
            self.update_table(tab)
            self.tabs.active = tab
        self.refresh_bindings()

    @handle_error
    def action_display_nodes(self):
        def toggle():
            self.only_available_nodes = not self.only_available_nodes
        self._display_tab("node", toggle)

    @handle_error
    def action_display_history_jobs(self):
        def toggle():
            self.only_completed_history = not self.only_completed_history
        self._display_tab("history", toggle)

    @handle_error
    def action_display_jobs(self):
        def toggle():
            self.only_running_jobs = not self.only_running_jobs
        self._display_tab("job", toggle)

    @handle_error
    def action_abort(self):
        action = self.STAGE["action"]
        if action == "delete":
            self.info_log.write("Delete: aborted")
            self.selected_jobid = []
            self.STAGE.pop("job_id", None)
        elif action == "job_log":
            self.job_log_position = None
            self.STAGE.pop("log_fn", None)
        elif action == "select":
            self.info_log.write("Select: none")
            self.selected_jobid = []
        elif action in ["node", "history"]:
            self.tabs.active = "job"
        action = self.tabs.active
        self.STAGE['action'] = action
        self.update_table(action)
        self.switch_display(action)
        self.refresh()

    def _toggle_selection(self, row, job_id):
        table = self.tables[self.active_table]
        if job_id in self.selected_jobid:
            self.selected_jobid.remove(job_id)
            table.update_cell_at((row, 0), job_id)
            return False
        self.selected_jobid.append(job_id)
        table.update_cell_at((row, 0), Text(job_id, style=self.selected_text_style))
        return True

    def _report_selection(self):
        if self.selected_jobid:
            self.STAGE["action"] = "select"
            self.info_log.write(f"Select: {' '.join(self.selected_jobid)}")
        else:
            self.STAGE["action"] = "job"
            self.info_log.write("Select: none")
        self.refresh_bindings()

    @handle_error
    def action_select(self):
        if self.STAGE["action"] == "job" and self.selected_jobid:
            return
        table = self.tables[self.active_table]
        self._toggle_selection(table.cursor_row, self._get_selected_job())
        self._report_selection()
        table.action_cursor_down()

    @handle_error
    def action_select_inverse(self):
        table = self.tables[self.active_table]
        for i in range(table.row_count):
            if self._toggle_selection(i, str(table.get_cell_at((i, 0)))):
                table.move_cursor(row=i, column=0)
        self._report_selection()

    @run_in_thread
    @handle_error
    def auto_refresh(self):
        if self.verbose:
            self.info_log.write("Auto-refreshing...")
        action = self.STAGE["action"]
        if action in ["job", "node"]:
            self.update_table(action)
        elif action == "job_log":
            self.update_log(self.STAGE["log_fn"])
        self.update_status()

    @handle_error
    def action_refresh(self):
        action = self.STAGE["action"]
        if action == "job_log":
            self.update_log(self.STAGE["log_fn"])
        else:
            self.rewrite_table(action, keep_state=True)
        self.update_status()

    @handle_error
    def action_confirm(self):
        result = subprocess.run(["scancel", *self.STAGE["job_id"].split()])
        self.info_log.write(f"Delete: {self.STAGE['job_id']}? {'succeeded' if result.returncode == 0 else 'failed'}")
        self.selected_jobid = []
        self.update_table("job")
        self.STAGE["action"] = "job"
        self.refresh_bindings()

    @handle_error
    def action_sort(self):
        stage = self.STAGE[self.STAGE["action"]]
        sort_column = self.tables[self.active_table].cursor_column
        if sort_column != stage.get("sort_column"):
            stage["sort_ascending"] = False
        else:
            stage["sort_ascending"] = not stage.get("sort_ascending", True)
        stage['sort_column'] = sort_column

        self.rewrite_table(self.active_table, keep_state=True)
        self.tables[self.active_table].move_cursor(row=0, column=sort_column)

    @handle_error
    def action_delete(self):
        if self.STAGE["action"] == "job":
            job_id = self._get_selected_job()
        else:
            job_id = ' '.join(self.selected_jobid)
        self.info_log.write(f"Delete: {job_id}? press <<ENTER>> to confirm")
        self.STAGE.update({"action": "delete", "job_id": job_id})
        self.refresh_bindings()

    @handle_error
    def action_print_gpustat(self):
        job_id = self._get_selected_job()
        gpustat = subprocess.check_output(["srun", "--jobid", job_id, "--overlap", "gpustat"], timeout=3, text=True)
        self.info_log.write(gpustat.rstrip())

    def _run_suspended(self, cmd):
        """Run an interactive command in the terminal while the UI is suspended."""
        with self.suspend():
            # Ignore SIGINT in slurman, but let the command receive it
            original_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                subprocess.run(cmd, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
            finally:
                signal.signal(signal.SIGINT, original_sigint)
        self.refresh()

    @handle_error
    def action_open_shell(self):
        job_id = self._get_selected_job()
        self._run_suspended(["srun", "--jobid", job_id, "--overlap", "--pty", os.environ.get("SHELL", "bash")])

    @handle_error
    def action_display_job_log(self):
        log_fn = self._get_log_fn(self._get_selected_job())
        assert os.path.exists(log_fn), f"Log file not found: {log_fn}"
        self.STAGE.update({"action": "job_log", "log_fn": log_fn})
        self.job_log_position = None
        self.update_log(log_fn)
        self.switch_display("job_log")
        self.refresh_bindings()

    @handle_error
    def action_open_with_less(self):
        log_fn = self.STAGE.get("log_fn") or self._get_log_fn(self._get_selected_job())
        assert os.path.exists(log_fn), f"Log file not found: {log_fn}"
        self._run_suspended(['less', '+G', log_fn])

    @handle_error
    def action_toggle_user_range(self):
        self.only_my_jobs = not self.only_my_jobs
        self.rewrite_table("job", keep_state=True)
        self.switch_display("job")

    @handle_error
    def action_toggle_history_range(self):
        if self.STAGE[self.active_table].get('updating'):
            return
        ranges = list(HISTORY_RANGES)
        idx = ranges.index(self.history_range) + 1 if self.history_range in ranges else 0
        self.history_range = ranges[idx % len(ranges)]
        self.rewrite_table("history", keep_state=True)
        self.switch_display("history")

    def print_tab_prompt(self, tab_id):
        if not self.verbose:
            self.info_log.clear()

        if tab_id == "node":
            info = f"Press 'g' to toggle nodes: {'Available' if self.only_available_nodes else 'All'}"
        elif tab_id == "history":
            info = f"Press 'h' to toggle job states: {'Completed' if self.only_completed_history else 'All'}\t| " \
            + f"Press 'H' to toggle history range: {self.history_range}"
        else:
            info = f"Press 'j' to toggle job states: {'Running' if self.only_running_jobs else 'All'}\t|" \
            + f" Press 'J' to toggle user range: {'Me' if self.only_my_jobs else 'All'}"
        self.info_log.write(info)

    def switch_display(self, action):
        if self.verbose:
            self.info_log.write(f"Switch display: {action}")
        if action == "job_log":
            for table in self.tables.values():
                table.styles.height = "0%"
            self.info_log.styles.height = "0%"
            self.info_log.styles.border = ("none", self.border[1])
            self.job_log.styles.height = "100%"
            self.job_log.focus()
        elif action in TABLES:
            for name, table in self.tables.items():
                table.styles.height = "80%" if name == action else "0%"
            self.active_table = action
            self.tables[action].focus()
            self.info_log.styles.height = "20%"
            self.info_log.styles.border = self.border
            self.print_tab_prompt(action)
            self.job_log.styles.height = "0%"
            self.job_log.clear()
        else:
            raise ValueError(f"Invalid action: {action}")

    @handle_error
    def update_status(self):
        stats = self.stats
        self.tab_jobs.label = f"Jobs: {stats.get('njobs_running', 0)}/{stats.get('njobs', 0)}"
        self.tab_nodes.label = f"GPUs: {stats.get('ngpus_avail', 0)}/{stats.get('ngpus', 0)}"
        self.tab_history.label = f"History: {stats.get('nhistory_completed', 0)}/{stats.get('nhistory', 0)}"
        self.tab_time.label = f"{HOSTNAME} | {datetime.now().strftime('%H:%M:%S')}"

    def _fetch_rows(self, table_type):
        """Query the data of a table and return its columns and rows as strings."""
        stage = self.STAGE[table_type]
        query = {"job": self.query_jobs, "node": self.query_gpus, "history": self.query_history}[table_type]
        df = query()
        if stage.get("sort_column") is not None:
            df = df.sort_values(df.columns[stage["sort_column"]], ascending=stage.get("sort_ascending", True))
        self.update_status()
        return list(df.columns), df.astype(str).values.tolist()

    @run_in_thread
    @handle_error
    def rewrite_table(self, table_type, keep_state=False):
        stage = self.STAGE[table_type]
        if stage.get('updating'):
            return
        stage['updating'] = True
        try:
            columns, rows = self._fetch_rows(table_type)

            table = self.tables[table_type]
            cursor_row, cursor_column = (table.cursor_row, table.cursor_column) if keep_state else (0, 0)
            table.clear(columns=True)
            table.add_columns(*columns)
            table.add_rows(rows)
            table.move_cursor(row=min(cursor_row, len(rows) - 1), column=min(cursor_column, len(columns) - 1))
            time.sleep(0.3)
        finally:
            stage['updating'] = False

    @run_in_thread
    @handle_error
    def update_table(self, table_type):
        stage = self.STAGE[table_type]
        if stage.get('updating'):
            return
        stage['updating'] = True
        try:
            columns, rows = self._fetch_rows(table_type)

            table = self.tables[table_type]
            if len(table.columns) != len(columns):
                table.clear(columns=True)
                table.add_columns(*columns)

            for row_index, row in enumerate(rows[:table.row_count]):
                for col_index, cell in enumerate(row):
                    if table.get_cell_at((row_index, col_index)) != cell:
                        table.update_cell_at((row_index, col_index), cell)
            table.add_rows(rows[table.row_count:])

            while table.row_count > len(rows):
                row_key, _ = table.coordinate_to_cell_key((table.row_count - 1, 0))
                table.remove_row(row_key)
        finally:
            stage['updating'] = False

    def _get_selected_job(self):
        table = self.tables[self.active_table]
        return str(table.get_row_at(table.cursor_row)[0])

    @handle_error
    def update_log(self, log_fn):
        self.job_log.border_title = log_fn
        follow = self.job_log.scroll_offset.y == self.job_log.max_scroll_y

        # Read logs in binary mode so non-UTF8 bytes don't crash decoding.
        with open(log_fn, "rb") as log_file:
            if self.job_log_position is None:
                # start with the last 32KB, beginning from a full line
                start = max(os.path.getsize(log_fn) - 2**15, 0)
                log_file.seek(start)
                new_data = log_file.read()
                if start > 0:
                    new_data = new_data[new_data.find(b"\n") + 1:]
            else:
                log_file.seek(self.job_log_position)
                new_data = log_file.read()
            self.job_log_position = log_file.tell()

        for line in new_data.decode("utf-8", errors="replace").splitlines(keepends=True):
            self.job_log.write(line)
        if follow:
            self.job_log.scroll_end(animate=False)

    def _get_log_fn(self, job_id):
        if self.STAGE["action"] == "history":
            output = self._run(["sacct", "-j", job_id, "--format=StdOut", "-P"])
            return output.split("\n")[1].strip().replace("%j", job_id)
        output = self._run(["scontrol", "show", "job", job_id])
        match = re.search(r"StdOut=(\S+)", output)
        assert match, f"StdOut not found for job {job_id}"
        return match.group(1)

    def _run(self, cmd):
        """Run a slurm command and return its output."""
        if self.verbose:
            self.info_log.write(" ".join(cmd))
        return subprocess.check_output(cmd, text=True)

    def query_jobs(self):
        df = self.get_squeue()
        self.stats['njobs'] = len(df)
        self.stats['njobs_running'] = int((df['STATE'] == 'RUNNING').sum())
        return df

    def query_gpus(self):
        df = self.get_sinfo()
        self.stats['ngpus'] = int(df["GPUs (Total)"].sum())
        self.stats['ngpus_avail'] = int(df["GPUs (Avail)"].sum())
        if self.only_available_nodes:
            df = df[df["GPUs (Avail)"] > 0]
        # hide columns for simplicity
        return df.drop(columns=["GPUs (Total)", "GPUs (Avail)"])

    def query_history(self):
        df = self.get_sacct()
        self.stats['nhistory'] = len(df)
        if self.only_completed_history:
            df = df[df["State"] == "COMPLETED"]
        self.stats['nhistory_completed'] = len(df)
        return df

    def get_squeue(self):
        fields = [
            "JOBID:18",
            "USERNAME:10",
            "PRIORITY:8",
            "PARTITION:80",
            "NAME:200",
            "STATE:8",
            "TimeUsed:10",
            "StartTime:30",
            "TimeLimit:15",
            "tres-alloc:100",
            "ReasonList:100",
        ]
        cmd = ["squeue", f"--Format={'|,'.join(fields)}", "-S", "T"]
        if self.only_my_jobs:
            cmd.append("--me")
        if self.only_running_jobs:
            cmd.append("--state=RUNNING")
        df = pd.read_csv(io.StringIO(re.sub(' +', '', self._run(cmd))), sep="|")

        if not df.empty:
            # right align time
            df["TIME"] = df["TIME"].str.rjust(df["TIME"].str.len().max())
            df["START_TIME"] = df["START_TIME"].map(simplify_start_time)
        return df

    def get_sinfo(self):
        fields = [
            "Partition:25",
            "NodeHost",
            "Gres:500",
            "GresUsed:500",
            "StateCompact",
            "FreeMem",
            "Memory",
            "CPUsState",
            "Features:200",
        ]
        output = self._run(["sinfo", "-O", ",".join(fields)])
        df = pd.read_csv(io.StringIO(re.sub(' +', ' ', output)), sep=" ")

        records = []
        for _, row in df.iterrows():
            if row['GRES'] == "(null)":
                continue
            device, ngpus = parse_gres(row['GRES'])
            gpu_avail_idx = parse_gres_used(row['GRES_USED'], ngpus) if row["STATE"] in ["mix", "idle", "alloc"] else []
            _, ncpus_avail, _, ncpus_total = row["CPUS(A/I/O/T)"].split("/")

            host_info = {
                'Partition': str(row["PARTITION"]),
                'Host': str(row["HOSTNAMES"]),
                'Device': device,
                'State': str(row["STATE"]),
                'Mem (GB)': f"{mem_gb(row['FREE_MEM'])}/{mem_gb(row['MEMORY'])}",
                'CPUs': f"{ncpus_avail}/{ncpus_total}",
                'GPUs (Total)': ngpus,
                'GPUs (Avail)': len(gpu_avail_idx),
                'GPUs': f"{len(gpu_avail_idx)}/{ngpus}",
                'GPUs (Avail IDX)': f"[{','.join(map(str, gpu_avail_idx))}]",
            }

            # named features ("name:value") get their own column
            unnamed_features = []
            for feature in str(row["AVAIL_FEATURES"]).split(","):
                name, sep, value = feature.partition(':')
                if sep:
                    host_info[name] = value
                else:
                    unnamed_features.append(feature)
            host_info['Feature'] = ','.join(unnamed_features)

            records.append(host_info)
        return pd.DataFrame.from_records(records).drop_duplicates("Host")

    def get_sacct(self, endtime="now"):
        days = HISTORY_RANGES.get(self.history_range)
        starttime = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d') if days else "2024-11-26"
        fields = "JobID,JobName,State,Start,Elapsed,NodeList,AllocTRES,Partition,StdOut"
        output = self._run(["sacct", f"--format={fields}", "-P", "-X", f"--starttime={starttime}", f"--endtime={endtime}"])
        df = pd.read_csv(io.StringIO(output), sep='|')
        df.columns = df.columns.str.strip()
        for col in df.select_dtypes(['object']).columns:
            df[col] = df[col].str.strip()
        return df


def parse_gres(gres_str):
    match = re.match(r"([^:]+)(?::([^:()]+))?:([^:(,]+)(?:\(S:([^)]+)\))?", gres_str)
    if not match:
        raise ValueError(f"Error parsing gres: {gres_str}")
    _, device, ngpus, _ = match.groups()
    return device, int(ngpus)

def parse_gres_used(gres_used_str, num_total):
    """Return the indices of the GPUs that are not in use."""
    match = re.match(r"([^:]+)(?::([^:]+))?:([^:(,]+)(?:\(IDX:([^)]+)\))?", gres_used_str)
    if not match:
        raise ValueError(f"Error parsing gres_used: {gres_used_str}")
    _, _, ngpus_used, alloc_str = match.groups()

    gpu_used_idx = set()
    if alloc_str:
        for gpu_ids in alloc_str.split(","):
            if gpu_ids == "N/A":
                continue
            start, _, end = gpu_ids.partition("-")
            gpu_used_idx.update(range(int(start), int(end or start) + 1))
        assert int(ngpus_used) == len(gpu_used_idx), f"Number of used GPUs {ngpus_used} does not match parsed indices {gpu_used_idx} in gres_used: {gres_used_str}"

    return [idx for idx in range(num_total) if idx not in gpu_used_idx]

def mem_gb(mem_mb):
    try:
        return int(mem_mb) // 1024
    except (TypeError, ValueError):
        return mem_mb

def simplify_start_time(start_time):
    """Remove the year from a start time"""
    try:
        return datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S").strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return start_time

def run_ui(verbose=False, interval=10, history_range="1 week"):
    app = Slurman()
    app.verbose = verbose
    app.interval = interval
    app.history_range = history_range
    app.run()


if __name__ == "__main__":
    run_ui()
