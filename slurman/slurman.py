from collections import OrderedDict
import io
import importlib.metadata
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Footer, RichLog, DataTable, Tabs, Tab
from textual.containers import Container
from rich.text import Text
import subprocess
import pandas as pd
import re
import os
import threading
import traceback
from functools import wraps
from datetime import datetime, timedelta
import time
import socket
import signal


DEBUG = False
if DEBUG:
    from slurman.debug_strings import SINFO_DEBUG, SQUEUE_DEBUG


ERROR_LOG_PATH = os.path.expanduser("~/.slurman_error.log")
ERROR_LOG_LOCK = threading.Lock()


def run_in_thread(func):
    """Decorator to run a function in a separate thread"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        thread = threading.Thread(target=func, args=args, kwargs=kwargs)
        thread.start()
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
    history_range = "1 week"  # Default history range

    # internal states
    STAGE = {
        "action": "job",
        "job": {
            # "sort_column": 0,
            # "sort_ascending": True,
        },
        "history": {
            "sort_column": 0,
            "sort_ascending": False,
        },
        "node": {
            # "sort_column": 0,
            # "sort_ascending": True,
        },
    }
    stats = {}
    selected_jobid = []
    only_available_nodes = True
    only_completed_history = False
    only_running_jobs = False
    only_my_jobs = True

    theme = "textual-dark"
    selected_text_style = "bold on orange3"
    border_type = "solid"
    border_color = "white"

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
        Binding("l", "display_job_log", "Log"),
        Binding("L", "open_with_less", "Open with less"),
        Binding("J", "toggle_user_range", "State"),
        Binding("H", "toggle_history_range", "Range"),
    ]

    def compose(self) -> ComposeResult:
        self.header = Header()
        self.footer = Footer()

        self.tab_nodes = Tab("GPUs", id="node")
        self.tab_history = Tab("History", id="history")
        self.tab_jobs = Tab("Jobs", id="job")
        self.tab_time = Tab("Time", id="time", disabled=True, classes="time-tab")
        self.tabs = Tabs(self.tab_nodes, self.tab_history, self.tab_jobs, self.tab_time, id="tabs")
        self.tabs.can_focus = False

        self.node_table = DataTable(id="node_table")
        self.job_table = DataTable(id="job_table")
        self.history_table = DataTable(id="history_table")
        self.tables = {
            "job": self.job_table,
            "node": self.node_table,
            "history": self.history_table
        }
        self.active_table = "job"

        self.info_log = RichLog(wrap=True, highlight=True, id="info_log", auto_scroll=True)
        self.info_log.can_focus = False
        self.info_log.border_title = "Info"
        
        self.job_log = RichLog(wrap=True, highlight=True, id="job_log", auto_scroll=False)
        self.job_log_position = None
        
        
        yield self.header
        yield self.tabs
        yield Container(self.job_table, self.node_table, self.history_table, self.info_log, self.job_log)
        yield self.footer

    def on_mount(self):
        pass

    def on_ready(self) -> None:
        self.rewrite_table("job")
        self.rewrite_table("node")
        self.rewrite_table("history")
        self.tabs.active = 'job'
        if self.interval > 0:
            self.set_interval(self.interval, self.auto_refresh)

    def on_tabs_tab_activated(self, message):
        tab_id = message.tab.id
        if self.verbose:
            self.info_log.write(f"Tab activated: {tab_id}")

        self.STAGE.update({"action": tab_id})
        self.update_table(tab_id)
        self.switch_display(tab_id)
        self.refresh_bindings()
        
    @handle_error
    def check_action(self, action: str, parameters):  
        """Check if an action may run."""
        if action == "display_nodes" and self.STAGE['action'] not in ['node', 'history', 'job']:
            return False
        elif action == "display_history_jobs" and self.STAGE['action'] not in ['node', 'history', 'job']:
            return False
        elif action == "display_jobs" and self.STAGE['action'] not in ['node', 'history', 'job']:
            return False
        elif action == "abort":
            pass
        elif action == "select" and self.STAGE['action'] not in ['job', 'select']:
            return False
        elif action == "select_inverse" and self.STAGE['action'] not in ['job', 'select']:
            return False
        elif action == "refresh" and self.STAGE['action'] not in ['job', 'history', 'node', 'job_log']:
            return False
        elif action == "confirm" and self.STAGE['action'] != 'delete':
            return False
        elif action == "sort" and self.STAGE['action'] not in ['job', 'history', 'node']:
            return False
        elif action == "delete" and self.STAGE['action'] not in ['job', 'select']:
            return False
        elif action == "print_gpustat" and self.STAGE['action'] != 'job':
            return False
        elif action == "display_job_log" and self.STAGE['action'] not in ['job', 'history']:
            return False
        elif action == "open_with_less" and self.STAGE['action'] not in ['job', 'job_log', 'history']:
            return False
        elif action == "toggle_user_range" and self.STAGE['action'] != 'job':
            return False
        elif action == "toggle_history_range" and self.STAGE['action'] != 'history':
            return False
        return True

    @handle_error
    def action_display_nodes(self):
        if self.STAGE[self.active_table]['updating']:
            return
        if self.STAGE["action"] in ["history", "job"]:
            self.STAGE.update({"action": "node"})
            self.update_table("node")
            self.refresh_bindings()
            self.tabs.active = "node"
        elif self.STAGE["action"] == "node":
            self.only_available_nodes = not self.only_available_nodes
            self.rewrite_table("node", keep_state=True)
            self.switch_display("node")

    @handle_error
    def action_display_history_jobs(self):
        if self.STAGE[self.active_table]['updating']:
            return
        if self.STAGE["action"] in ["node", "job"]:
            self.STAGE.update({"action": "history"})
            self.update_table("history")
            self.refresh_bindings()
            self.tabs.active = "history"
        elif self.STAGE["action"] == "history":
            self.only_completed_history = not self.only_completed_history
            self.rewrite_table("history", keep_state=True)
            self.switch_display("history")
    
    @handle_error
    def action_display_jobs(self):
        if self.STAGE[self.active_table]['updating']:
            return
        if self.STAGE["action"] in ["node", "history"]:
            self.STAGE.update({"action": "job"})
            self.update_table("job")
            self.refresh_bindings()
            self.tabs.active = "job"
        elif self.STAGE["action"] == "job":
            self.only_running_jobs = not self.only_running_jobs
            self.rewrite_table("job", keep_state=True)
            self.refresh_bindings()
            self.switch_display("job")

    @handle_error
    def action_abort(self):
        if self.STAGE["action"] == "delete":
            self.info_log.write("Delete: aborted")
            self.selected_jobid = []
            self.STAGE.pop("job_id", None)
            self.STAGE.pop("job_name", None)
        elif self.STAGE["action"] == "job_log":
            self.job_log_position = None
            self.STAGE.pop("job_id", None)
            self.STAGE.pop("job_name", None)
            self.STAGE.pop("log_fn", None)
        elif self.STAGE["action"] == "select":
            self.info_log.write("Select: none")
            self.selected_jobid = []
        elif self.STAGE["action"] in ["node", "history"]:
            self.tabs.active = "job"
        action = self.tabs.active
        self.STAGE['action'] = action
        self.update_table(action)
        self.switch_display(action)
        self.refresh()

    @handle_error
    def action_select(self):
        if (self.STAGE["action"] == "job" and not self.selected_jobid) or self.STAGE["action"] == "select":
            i = self.tables[self.active_table].cursor_coordinate[0]
            value = str(self.tables[self.active_table].get_cell_at((i, 0)))

            job_id = self._get_selected_job()
            if job_id in self.selected_jobid:
                self.selected_jobid.remove(job_id)
                self.tables[self.active_table].update_cell_at((i, 0), value)
            else:
                self.selected_jobid.append(job_id)
                self.tables[self.active_table].update_cell_at((i, 0), Text(str(value), style=self.selected_text_style))
            
            if self.selected_jobid:
                self.STAGE["action"] = "select"
                self.info_log.write(f"Select: {' '.join(self.selected_jobid)}")
            else:
                self.STAGE["action"] = "job"
                self.info_log.write(f"Select: none")
            self.tables[self.active_table].action_cursor_down()
        self.refresh_bindings()  
   
    @handle_error
    def action_select_inverse(self):
        assert self.STAGE["action"] in ["job", "select"]
        for i in range(len(self.tables[self.active_table].rows)):
            job_id = str(self.tables[self.active_table].get_cell_at((i, 0)))
            
            if job_id in self.selected_jobid:
                self.selected_jobid.remove(job_id)
                self.tables[self.active_table].update_cell_at((i, 0), job_id)
            else:
                self.selected_jobid.append(job_id)
                self.tables[self.active_table].update_cell_at((i, 0), Text(str(job_id), style=self.selected_text_style))
                self.tables[self.active_table].move_cursor(row=i, column=0)
        if self.selected_jobid:
            self.STAGE["action"] = "select"
            self.info_log.write(f"Select: {' '.join(self.selected_jobid)}")
        else:
            self.STAGE["action"] = "job"
            self.info_log.write(f"Select: none")
        self.refresh_bindings()  
    
    @run_in_thread
    @handle_error
    def auto_refresh(self):
        if self.verbose:
            self.info_log.write("Auto-refreshing...")
        if self.STAGE["action"] == "job":
            self.update_table("job")
        elif self.STAGE["action"] == "job_log":
            self.update_log(self.STAGE["log_fn"])
        elif self.STAGE["action"] == "node":
            self.update_table("node")
        # elif self.STAGE["action"] == "history":
        #     self.update_table("history")
        self.update_status()

    @handle_error
    def action_refresh(self):
        if self.STAGE["action"] == "job":
            self.rewrite_table("job", keep_state=True)
        elif self.STAGE["action"] == "job_log":
            self.update_log(self.STAGE["log_fn"])
        elif self.STAGE["action"] == "node":
            self.rewrite_table("node", keep_state=True)
        elif self.STAGE["action"] == "history":
            self.rewrite_table("history", keep_state=True)
        self.update_status()
    
    @handle_error
    def action_confirm(self):
        # job to delete
        if self.STAGE["action"] == "delete":
            perform_scancel(self.STAGE['job_id'])
            self.info_log.write(f"Delete: {self.STAGE['job_id']}? succeeded")
            self.selected_jobid = []
            self.update_table("job")
            self.STAGE["action"] = "job"
        self.refresh_bindings()

    @handle_error
    def action_sort(self):
        sort_column = self.tables[self.active_table].cursor_column
        if sort_column != self.STAGE[self.STAGE["action"]].get("sort_column"):
            self.STAGE[self.STAGE["action"]]["sort_ascending"] = False
        else:
            self.STAGE[self.STAGE["action"]]["sort_ascending"] = not self.STAGE[self.STAGE["action"]].get("sort_ascending", True)
        self.STAGE[self.STAGE["action"]]['sort_column'] = sort_column

        self.rewrite_table(self.active_table, keep_state=True)
        self.tables[self.active_table].move_cursor(row=0, column=sort_column)
    
    @handle_error
    def action_delete(self):
        if self.STAGE["action"] == "job":
            job_id = self._get_selected_job()
            self.info_log.write(f"Delete: {job_id}? press <<ENTER>> to confirm")
            self.STAGE.update({"action": "delete", "job_id": job_id})
        elif self.STAGE["action"] == "select":
            self.info_log.write(f"Delete: {' '.join(self.selected_jobid)}? press <<ENTER>> to confirm")
            self.STAGE.update({"action": "delete", "job_id": ' '.join(self.selected_jobid)})
        self.refresh_bindings()  

    @handle_error
    def action_print_gpustat(self):
        if self.STAGE["action"] == "job":
            job_id = self._get_selected_job()
            gpustat = subprocess.check_output(f"""srun --jobid {job_id} gpustat""", shell=True, timeout=3).decode("utf-8").rstrip()
            self.info_log.write(gpustat)
    
    @handle_error
    def action_display_job_log(self):
        if self.STAGE["action"] in ["job", "history"]:
            job_id = self._get_selected_job()
            log_fn = self._get_log_fn(job_id)
            assert os.path.exists(log_fn), f"Log file not found: {log_fn}"
            self.STAGE.update({"action": "job_log", "log_fn": log_fn})
            self.job_log_position = None
            self.update_log(log_fn)
            self.switch_display("job_log")
        self.refresh_bindings()
    
    @handle_error
    def action_open_with_less(self):
        if self.STAGE["action"] in ["job", "job_log", "history"]:
            if 'log_fn' not in self.STAGE:
                job_id = self._get_selected_job()
                log_fn = self._get_log_fn(job_id)
            else:
                log_fn = self.STAGE['log_fn']
            assert os.path.exists(log_fn), f"Log file not found: {log_fn}"
            with self.suspend():
                # Save the current SIGINT handler
                original_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    subprocess.run(['less', '+G', log_fn])
                finally:
                    # Restore the original SIGINT handler
                    signal.signal(signal.SIGINT, original_sigint)
            self.refresh()
    
    @handle_error
    def action_toggle_user_range(self):
        if self.STAGE["action"] == "job":
            self.only_my_jobs = not self.only_my_jobs
            self.rewrite_table("job", keep_state=True)
            self.refresh_bindings()
            self.switch_display("job")

    @handle_error
    def action_toggle_history_range(self):
        if self.STAGE[self.active_table]['updating']:
            return
        if self.history_range == "1 week":
            self.history_range = "1 month"
        elif self.history_range == "1 month":
            self.history_range = "4 months"
        elif self.history_range == "4 months":
            self.history_range = "1 year"
        else:
            self.history_range = "1 week"
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
        elif tab_id == "job":
            info = f"Press 'j' to toggle job states: {'Running' if self.only_running_jobs else 'All'}\t|" \
            + f" Press 'J' to toggle user range: {'Me' if self.only_my_jobs else 'All'}"
        self.info_log.write(info)

    def switch_display(self, action):
        if self.verbose:
            self.info_log.write(f"Switch display: {action}")
        if action == "node":
            self.node_table.styles.height = "80%"
            self.active_table = action
            self.tables[self.active_table].focus()
            self.info_log.styles.height="20%"
            self.info_log.styles.border = (self.border_type, self.border_color)
            self.print_tab_prompt(action)
        
            self.job_table.styles.height = "0%"
            self.history_table.styles.height = "0%"
            self.job_log.styles.height="0%"
            self.job_log.styles.border = (self.border_type, self.border_color)
            self.job_log.clear()
        elif action == "history":
            self.history_table.styles.height = "80%"
            self.active_table = action
            self.tables[self.active_table].focus()
            self.info_log.styles.height="20%"
            self.info_log.styles.border = (self.border_type, self.border_color)
            self.print_tab_prompt(action)

            self.job_table.styles.height = "0%"
            self.node_table.styles.height = "0%"
            self.job_log.styles.height="0%"
            self.job_log.styles.border = (self.border_type, self.border_color)
            self.job_log.clear()
        elif action == "job":
            self.job_table.styles.height = "80%"
            self.active_table = action
            self.tables[self.active_table].focus()
            self.info_log.styles.border = (self.border_type, self.border_color)
            self.info_log.styles.height="20%"
            self.print_tab_prompt(action)

            self.history_table.styles.height = "0%"
            self.node_table.styles.height = "0%"
            self.job_log.styles.border = (self.border_type, self.border_color)
            self.job_log.styles.height="0%"
            self.job_log.clear()
        elif action == "job_log":
            self.job_log.styles.height="100%"
            self.job_log.styles.border = (self.border_type, self.border_color)
            self.job_log.focus()
            
            self.job_table.styles.height="0%"
            self.node_table.styles.height="0%"
            self.history_table.styles.height="0%"
            self.info_log.styles.height="0%"
            self.info_log.styles.border = ("none", self.border_color)
        else:
            raise ValueError(f"Invalid action: {action}")

    @run_in_thread
    @handle_error
    def update_status(self):
        self.title = f"Slurman (v{importlib.metadata.version('slurman')})"

        njobs = self.stats.get("njobs", 0)
        njobs_running = self.stats.get("njobs_running", 0)
        self.tab_jobs.label = f"Jobs: {njobs_running}/{njobs}"

        ngpus_avail = self.stats.get("ngpus_avail", 0)
        ngpus = self.stats.get("ngpus", 0)
        self.tab_nodes.label = f"GPUs: {ngpus_avail}/{ngpus}"

        nhistory = self.stats.get("nhistory", 0)
        nhistory_completed = self.stats.get("nhistory_completed", 0)
        self.tab_history.label = f"History: {nhistory_completed}/{nhistory}"
        
        self.tab_time.label = f"{socket.gethostname()} | {datetime.now().strftime('%H:%M:%S')}"

    @handle_error
    def query_jobs(self, sort_column=None, sort_ascending=True):
        squeue_df = self.get_squeue(self.only_my_jobs, self.only_running_jobs) 
        if sort_column is not None:
            squeue_df = squeue_df.sort_values(squeue_df.columns[sort_column], ascending=sort_ascending)

        self.stats['njobs'] = len(squeue_df)
        self.stats['njobs_running'] = sum(1 for row in squeue_df.iterrows() if row[1]['STATE'] == 'RUNNING')
        return squeue_df

    @run_in_thread
    @handle_error
    def rewrite_table(self, table_type, keep_state=False):
        if table_type not in self.STAGE:
            self.STAGE[table_type] = {}
        if self.STAGE[table_type].get('updating', False):
            return
        self.STAGE[table_type]['updating'] = True

        if 'sort_column' in self.STAGE[table_type]:
            sort_column = self.STAGE[table_type]['sort_column']
        else:
            sort_column = None
        if 'sort_ascending' in self.STAGE[table_type]:
            sort_ascending = self.STAGE[table_type]['sort_ascending']
        else:
            sort_ascending = True

        df = self.query_table_data(table_type, sort_column, sort_ascending)
        self.update_status()

        table = self.tables[table_type]
        if keep_state:
            cursor_column = table.cursor_column
            cursor_row = table.cursor_row
        else:
            cursor_column = 0
            cursor_row = 0

        table.clear(columns=True)
        table.add_columns(*df.columns)
        
        for _, row in df.iterrows():
            table_row = [str(row[col]) for col in df.columns]
            table.add_row(*table_row)

        cursor_row = min(cursor_row, len(table.rows) - 1)
        cursor_column = min(cursor_column, len(table.columns) - 1)
        table.move_cursor(row=cursor_row, column=cursor_column)

        time.sleep(0.3)
        self.STAGE[table_type]['updating'] = False

    @run_in_thread
    @handle_error
    def update_table(self, table_type):
        if self.STAGE[table_type].get('updating', False):
            return
        self.STAGE[table_type]['updating'] = True
        if 'sort_column' in self.STAGE[table_type]:
            sort_column = self.STAGE[table_type]['sort_column']
        else:
            sort_column = None
        if 'sort_ascending' in self.STAGE[table_type]:
            sort_ascending = self.STAGE[table_type]['sort_ascending']
        else:
            sort_ascending = True

        df = self.query_table_data(table_type, sort_column, sort_ascending)
        self.update_status()

        table = self.tables[table_type]
        if not table.columns:
            table.add_columns(*df.columns)
            for _, row in df.iterrows():
                table_row = [str(row[col]) for col in df.columns]
                table.add_row(*table_row)
            return

        for row_index, (_, row) in enumerate(df.iterrows()):
            table_row = [str(row[col]) for col in df.columns]
            if row_index < len(table.rows):
                for col_index, cell in enumerate(table_row):
                    if table.get_cell_at((row_index, col_index)) != cell:
                        table.update_cell_at((row_index, col_index), cell)
            else:
                table.add_row(*table_row)

        while len(table.rows) > len(df):
            row_key, _ = table.coordinate_to_cell_key((len(table.rows) - 1, 0))
            table.remove_row(row_key)

        self.STAGE[table_type]['updating'] = False

    @handle_error
    def query_table_data(self, table_type, sort_column=None, sort_ascending=True):
        if table_type == "job":
            return self.query_jobs(sort_column, sort_ascending)
        elif table_type == "node":
            return self.query_gpus(sort_column, sort_ascending)
        elif table_type == "history":
            return self.query_history(sort_column, sort_ascending)
        else:
            raise ValueError(f"Invalid table type: {table_type}")

    @handle_error
    def _get_selected_job(self):
        row_idx = self.tables[self.active_table].cursor_row
        row = self.tables[self.active_table].get_row_at(row_idx)
        job_id = str(row[0])
        return job_id

    @handle_error
    def update_log(self, log_fn):
        self.job_log.border_title = f"{log_fn}"
        current_scroll_y = self.job_log.scroll_offset[1]

        # Read logs in binary mode so non-UTF8 bytes don't crash decoding.
        with open(log_fn, "rb") as log_file:
            if self.job_log_position is None:
                file_size = os.path.getsize(log_fn)
                self.job_log_position = max(file_size - 2**15, 0)  # read the last 4KB
                log_file.seek(self.job_log_position)
                new_data = log_file.read()
                if self.job_log_position > 0:
                    first_newline = new_data.find(b"\n")
                    if first_newline != -1:
                        new_data = new_data[first_newline + 1:]
            else:
                log_file.seek(self.job_log_position)
                new_data = log_file.read()
            self.job_log_position = log_file.tell()

        new_lines = new_data.decode("utf-8", errors="replace").splitlines(keepends=True)

        update_scroll = current_scroll_y == self.job_log.max_scroll_y
        
        for line in new_lines:
            self.job_log.write(line)

        if update_scroll:
            self.job_log.scroll_end(animate=False)
    
    @handle_error
    def _get_log_fn(self, job_id):
        if self.STAGE["action"] == "history":
            response_string = subprocess.check_output(f"""sacct -j {job_id} --format=StdOut -P""", shell=True).decode("utf-8")
            formatted_string = response_string.split("\n")[1].strip()
            formatted_string = formatted_string.replace("%j", job_id)
        elif self.STAGE["action"] in ["job", "job_log"]:
            response_string = subprocess.check_output(f"""scontrol show job {job_id} | grep StdOut""", shell=True).decode("utf-8")
            formatted_string = response_string.split("=")[-1].strip()
        else:
            raise ValueError(f"Cannot get log file for action: {self.STAGE['action']}")
        return formatted_string

    @handle_error
    def query_gpus(self,  sort_column=None, sort_ascending=True):
        overview_df = self.get_sinfo()
        self.stats['ngpus'] = overview_df["GPUs (Total)"].sum()
        self.stats['ngpus_avail'] = overview_df["GPUs (Avail)"].sum()
        if self.only_available_nodes:
            # filter out nodes with no available GPUs
            overview_df = overview_df[overview_df["GPUs (Avail)"] > 0]
        
        # hide columns for simplicity
        overview_df = overview_df.drop(columns=["GPUs (Total)", "GPUs (Avail)"])
        
        if sort_column is not None:
            overview_df = overview_df.sort_values(overview_df.columns[sort_column],ascending=sort_ascending)
        return overview_df

    @handle_error
    def query_history(self, sort_column=None, sort_ascending=True):
        starttime = self.get_history_starttime()
        sacct_df = self.get_sacct(starttime=starttime)
        if sort_column is not None:
            sacct_df = sacct_df.sort_values(sacct_df.columns[sort_column], ascending=sort_ascending)
        
        self.stats['nhistory'] = len(sacct_df)

        if self.only_completed_history:
            sacct_df = sacct_df[sacct_df["State"] == "COMPLETED"]

        self.stats['nhistory_completed'] = len(sacct_df)
        return sacct_df

    @handle_error
    def get_history_starttime(self):
        if self.history_range == "1 week":
            return (datetime.now() - timedelta(weeks=1)).strftime('%Y-%m-%d')
        elif self.history_range == "1 month":
            return (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        elif self.history_range == "4 months":
            return (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
        elif self.history_range == "1 year":
            return (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        else:
            return "2024-11-26"
    
    @handle_error
    def get_squeue(self, only_my_jobs=True, only_running_jobs=False):
        sep = "|"
        if DEBUG:
            response_string = SQUEUE_DEBUG
        else:
            args = f"{sep},".join([
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
            ])
            query_string = f"""squeue --Format="{args}" -S T"""
            if self.verbose:
                self.info_log.write(query_string)

            if only_my_jobs:
                query_string += " --me"
            if only_running_jobs:
                query_string += " --state=RUNNING"
            response_string = subprocess.check_output(query_string, shell=True).decode("utf-8")
        compact_string = re.sub(' +', '', response_string)
        data = io.StringIO(compact_string)
        df = pd.read_csv(data, sep=sep)

        # right align time
        max_length = df["TIME"].str.len().max()
        df.loc[:, "TIME"] = df.loc[:, "TIME"].apply(lambda x: f"{x:>{max_length}}")
        
        # remove years from start time
        df.loc[:, "START_TIME"] = df.loc[:, "START_TIME"].apply(lambda x: simplify_start_time(x))
        
        # simplify tres
        # df.loc[:, "TRES_ALLOC"] = df.loc[:, "TRES_ALLOC"].apply(simplify_tres)
        return df 
    
    @handle_error
    def get_sinfo(self):
        if DEBUG:
            response_string = SINFO_DEBUG
        else:
            args = f",".join([
                "Partition:25",
                "NodeHost",
                "Gres:500",
                "GresUsed:500",
                "StateCompact",
                "FreeMem",
                "Memory",
                "CPUsState",
                "Features:200"
            ])
            query_string = f"""sinfo -O {args}"""
            if self.verbose:
                self.info_log.write(query_string)

            response_string = subprocess.check_output(query_string, shell=True).decode("utf-8")

        formatted_string = re.sub(' +', ' ', response_string)
        data = io.StringIO(formatted_string)
        df = pd.read_csv(data, sep=" ")
        overview_df = []
        for row in df.iterrows():
            # overview_df = overview_df[['Partition', 'Host', "Device", "State", "Mem (GB)", "CPUs", "GPUs", "Free IDX", "Feature"]]

            if row[1]['GRES'] != "(null)":
                device, ngpus = self.parse_gres(row[1]['GRES'])
            else:
                continue
            
            node_available = row[1]["STATE"] in ["mix", "idle", "alloc"]
            if not node_available:
                gpu_avail_idx = []
            else:
                device, gpu_avail_idx = self.parse_gres_used(row[1]['GRES_USED'], ngpus)
            ngpus_avail = len(gpu_avail_idx)

            host_info = OrderedDict()

            host_info['Partition'] = str(row[1]["PARTITION"])
            host_info['Host'] = str(row[1]["HOSTNAMES"])
            host_info['Device'] = device
            host_info['State'] = str(row[1]["STATE"])

            try:
                mem_avail = int(row[1]["FREE_MEM"]) // 1024
            except:
                mem_avail = row[1]["FREE_MEM"]
            # host_info['Mem (Avail)'] = mem_avail
            try:
                mem_total = int(row[1]["MEMORY"]) // 1024
            except:
                mem_total = row[1]["MEMORY"]
            # host_info['Mem (Total)'] = mem_total
            host_info['Mem (GB)'] = f"{mem_avail}/{mem_total}"

            cpu_info = row[1]["CPUS(A/I/O/T)"].split("/")
            ncpus_avail = cpu_info[1]
            ncpus_total = cpu_info[3]
            # host_info['CPUs (Avail)'] = ncpus_avail
            # host_info['CPUs (Total)'] = ncpus_total
            host_info['CPUs'] = f"{ncpus_avail}/{ncpus_total}"

            host_info['GPUs (Total)'] = ngpus
            host_info['GPUs (Avail)'] = ngpus_avail
            host_info["GPUs"] = f"{host_info['GPUs (Avail)']}/{ngpus}"
            host_info['GPUs (Avail IDX)'] = f"[{','.join(str(idx) for idx in gpu_avail_idx)}]"

            features = row[1]["AVAIL_FEATURES"]
            if ',' in features:
                unnamed_features = []
                for feature in features.split(","):
                    if ':' in feature:
                        name, value = feature.split(':')
                        host_info[name] = value
                    else:
                        unnamed_features.append(feature)
            else:
                unnamed_features = [features]
            host_info['Feature'] = ','.join(unnamed_features)

            overview_df.append(host_info)
        overview_df = pd.DataFrame.from_records(overview_df).drop_duplicates("Host")
        return overview_df
    
    @handle_error
    def parse_gres(self, gres_str):
        match = re.match(r"([^:]+)(?::([^:()]+))?:([^:(,]+)(?:\(S:([^)]+)\))?", gres_str)

        if match:
            groups = match.groups()
            if self.verbose:
                self.info_log.write(f"Parsed gres: {groups} from {gres_str}")
            _, device, ngpus, _ = groups
            ngpus = int(ngpus)
        else:
            error_msg = f"Error parsing gres: {gres_str}"
            raise ValueError(error_msg)

        return device, ngpus

    @handle_error
    def parse_gres_used(self, gres_used_str, num_total):
        match = re.match(r"([^:]+)(?::([^:]+))?:([^:(,]+)(?:\(IDX:([^)]+)\))?", gres_used_str)
        if match:
            groups = match.groups()
            if self.verbose:
                self.info_log.write(f"Parsed gres_used: {groups} from {gres_used_str}")
            _, device, ngpus_used, alloc_str = groups
            ngpus_used = int(ngpus_used)
        else:
            error_msg = f"Error parsing gres_used: {gres_used_str}"
            raise ValueError(error_msg)

        gpu_used_idx = []
        if alloc_str:
            for gpu_ids in alloc_str.split(","):
                if "-" in gpu_ids:
                    start, end = gpu_ids.split("-")
                    for i in range(int(start), int(end)+1):
                        gpu_used_idx.append(i)
                else:
                    if gpu_ids == "N/A":
                        pass
                    else:
                        gpu_used_idx.append(int(gpu_ids))
            assert ngpus_used == len(gpu_used_idx), f"Number of used GPUs {ngpus_used} does not match parsed indices {gpu_used_idx} in gres_used: {gres_used_str}"

        gpu_avail_idx = [idx for idx in range(num_total) if idx not in gpu_used_idx]
        return device, gpu_avail_idx

    @handle_error
    def get_sacct(self, starttime="2024-11-26", endtime="now"):
        args = f",".join([
            "JobID",
            "JobName",
            "State",
            "Start",
            "Elapsed",
            "NodeList",
            "AllocTRES",
            "Partition",
            "StdOut"
        ])
        query_string = f"""sacct --format={args} -P -X --starttime={starttime} --endtime={endtime}"""
        if self.verbose:
            self.info_log.write(query_string)

        response_string = subprocess.check_output(
            query_string,
            shell=True
        ).decode("utf-8")
        data = io.StringIO(response_string)
        df = pd.read_csv(data, sep='|')

        # Strip whitespace from column names
        df.columns = df.columns.str.strip()

        # Strip whitespace from each string element in the DataFrame
        for col in df.select_dtypes(['object']).columns:
            df[col] = df[col].str.strip()
        return df

def perform_scancel(job_id):
    os.system(f"""scancel {job_id}""")

def remove_first_line(input_string):
    lines = input_string.split('\n')
    return '\n'.join(lines[1:])

def simplify_start_time(start_time):
    try:
        if start_time != "nan":
            start_time = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S").strftime("%m-%d %H:%M")
    except Exception as e:
        pass
    return start_time

def simplify_tres(tres):
    tres_ = []
    for x in str(tres).split(","):
        if 'billing=' in x:
            continue
        tres_.append(x)
    return ",".join(tres_)

def read_log(fn, num_lines=100):
    with open(os.path.expanduser(fn), 'r') as f:
        txt_lines = list(f.readlines()[-num_lines:])
    
    return txt_lines

def run_ui(verbose=False, interval=10, history_range="1 week"):
    # if debug:
    #     # global for quick debugging
    #     global DEBUG
    #     DEBUG = True
    app = Slurman()
    app.verbose = verbose
    app.interval = interval
    app.history_range = history_range
    app.run()


if __name__ == "__main__":
    run_ui()
