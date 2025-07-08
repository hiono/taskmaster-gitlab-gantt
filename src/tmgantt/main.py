"""
Gantt Chart Generator from Taskmaster & GitLab

This script generates an interactive Gantt chart in HTML format based on data from
Taskmaster tasks and GitLab issues.

Configuration is loaded from a `.env` file (GitLab base URL, personal access token, project ID, SSL verification setting, and optional overall Gantt start date).

Refer to `taskmaster-gitlab-gantt-spec.md` for detailed specifications.
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import gitlab
import holidays
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dotenv import dotenv_values
from vibelogger import VibeLoggerConfig, create_logger

# --- Constants & Settings ---
# TASKS_PATH = Path("/workspace/.taskmaster/tasks/tasks.json") # Moved to main
# DEFAULT_OUTPUT_HTML_PATH = Path("/workspace/gantt_chart.html") # Moved to main

# --- Logger Setup ---
# Configure VibeLogger to save logs to a file and keep them in memory for AI analysis
vibe_config = VibeLoggerConfig(
    log_file="./logs/tmgantt_vibe.log",  # Specify a log file path
    max_file_size_mb=10,  # Max 10MB per log file
    auto_save=True,
    keep_logs_in_memory=True,
    max_memory_logs=1000,  # Keep last 1000 logs in memory
)
logger = create_logger(config=vibe_config)


# --- Data Acquisition & Processing Modules ---


def load_taskmaster_tasks(tag="master"):
    """Loads and flattens tasks from the Taskmaster JSON file."""
    all_tasks = {}
    try:
        # TASKS_PATH is now passed as an argument or defined locally
        tasks_path = Path("/workspace/.taskmaster/tasks/tasks.json")
        with open(tasks_path, "r", encoding="utf-8") as f:
            tasks_data = json.load(f)

        if tag in tasks_data and "tasks" in tasks_data[tag]:

            def flatten(tasks_list, parent_id=None):
                for task in tasks_list:
                    current_id = f"{parent_id}.{task['id']}" if parent_id else str(task["id"])
                    all_tasks[current_id] = task
                    if "subtasks" in task and task.get("subtasks"):
                        flatten(task["subtasks"], current_id)

            flatten(tasks_data[tag]["tasks"])
            logger.info(
                operation="load_taskmaster_tasks",
                message=f"Successfully loaded {len(all_tasks)} tasks from Taskmaster.",
                context={"num_tasks": len(all_tasks)},
            )
        else:
            logger.warning(
                operation="load_taskmaster_tasks",
                message=f"Tag '{tag}' not found in {tasks_path}. No tasks will be loaded.",
                context={"tag": tag, "tasks_path": str(tasks_path)},
            )
    except FileNotFoundError:
        logger.error(
            operation="load_taskmaster_tasks",
            message=f"Taskmaster file not found at {tasks_path}",
            context={"tasks_path": str(tasks_path)},
        )
    except json.JSONDecodeError:
        logger.error(
            operation="load_taskmaster_tasks",
            message=f"Failed to parse Taskmaster JSON file: {tasks_path}",
            context={"tasks_path": str(tasks_path)},
        )
    except Exception as e:
        logger.error(
            operation="load_taskmaster_tasks",
            message=f"An unexpected error occurred while loading Taskmaster tasks: {e}",
            context={"error": str(e)},
        )
    return all_tasks


def get_gitlab_issues(gl, project_id):
    """Fetches all issues from the specified GitLab project."""
    issues = []
    project_name = "Unknown Project"
    try:
        project = gl.projects.get(project_id)
        project_name = project.name_with_namespace  # Get project name
        logger.info(operation="get_gitlab_issues", message=f"Accessing GitLab project: '{project_name}'")
        issues = project.issues.list(all=True)
        logger.info(
            operation="get_gitlab_issues",
            message=f"Successfully fetched {len(issues)} issues from GitLab.",
            context={"num_issues": len(issues)},
        )
    except gitlab.exceptions.GitlabError as e:
        logger.error(
            operation="get_gitlab_issues", message=f"GitLab API error while fetching issues: {e}", context={"error": str(e)}
        )
    except Exception as e:
        logger.error(
            operation="get_gitlab_issues",
            message=f"An unexpected error occurred while fetching GitLab issues: {e}",
            context={"error": str(e)},
        )
    return issues, project_name  # Return project_name as well


def map_tasks_and_issues(gitlab_issues):
    """Creates a mapping from Taskmaster task ID to GitLab issue object."""
    mapping = {}
    for issue in gitlab_issues:
        match = re.match(r"^([0-9\.]+):", issue.title)
        if match:
            mapping[match.group(1)] = issue
    logger.debug(
        operation="map_tasks_and_issues",
        message=f"Mapped {len(mapping)} GitLab issues to Taskmaster IDs.",
        context={"num_mapped_issues": len(mapping)},
    )
    return mapping


def is_working_day(d: date, country_holidays) -> bool:
    """Checks if a given date is a working day (not a weekend or a holiday)."""
    return d.weekday() < 5 and d not in country_holidays


def get_next_working_day(d: date, country_holidays) -> date:
    """Returns the next working day after a given date."""
    next_day = d + timedelta(days=1)
    while not is_working_day(next_day, country_holidays):
        next_day += timedelta(days=1)
    return next_day


def parse_task_list(description):
    """
    Parses Markdown task lists from a description string.
    """
    if not description:
        return []
    task_item_pattern = re.compile(r"^[ \t]*- \[([ |x])\] (.*)$", re.MULTILINE)
    tasks = []
    for line in description.splitlines():
        match = task_item_pattern.match(line)
        if match:
            completed = match.group(1).lower() == "x"
            title = match.group(2).strip()
            tasks.append({"title": title, "completed": completed})
    return tasks


def prepare_gantt_data(taskmaster_tasks, task_id_to_issue, overall_start_date, country_holidays):
    """Prepares and processes data into a pandas DataFrame for Plotly using ASAP scheduling."""
    df_data = []
    today = datetime.now().date()

    # --- Initial Date Determination ---
    # Find the earliest created_at among all GitLab issues if overall_start_date is not set
    earliest_created_at = None
    if not overall_start_date:
        for issue in task_id_to_issue.values():
            if issue and issue.created_at:
                issue_created_at = datetime.fromisoformat(issue.created_at).date()
                if earliest_created_at is None or issue_created_at < earliest_created_at:
                    earliest_created_at = issue_created_at
        if earliest_created_at:
            logger.info(
                operation="prepare_gantt_data",
                message=f"No overall_start_date. Using earliest GitLab issue created_at: {earliest_created_at}",
                context={"earliest_created_at": str(earliest_created_at)},
            )
        else:
            logger.warning(
                operation="prepare_gantt_data",
                message="Could not determine earliest created_at from GitLab issues. Falling back to today for initial start dates.",
            )

    # Initialize task_dates with end_dates first
    task_dates = {}
    for tm_id, tm_task in taskmaster_tasks.items():
        issue = task_id_to_issue.get(tm_id)
        end_date = None

        # Determine end_date
        if tm_task.get("status") == "done" and issue and issue.closed_at:
            end_date = datetime.fromisoformat(issue.closed_at).date()
            logger.debug(
                operation="prepare_gantt_data",
                message=f"Task {tm_id}: Using closed_at for end_date: {end_date}",
                context={"task_id": tm_id, "end_date": str(end_date), "source": "closed_at"},
            )
        elif issue and issue.due_date:
            end_date = datetime.strptime(issue.due_date, "%Y-%m-%d").date()
            logger.debug(
                operation="prepare_gantt_data",
                message=f"Task {tm_id}: Using due_date for end_date: {end_date}",
                context={"task_id": tm_id, "end_date": str(end_date), "source": "due_date"},
            )
        else:
            end_date = today + timedelta(days=7)  # Fallback
            logger.debug(
                operation="prepare_gantt_data",
                message=f"Task {tm_id}: Using fallback end_date: {end_date}",
                context={"task_id": tm_id, "end_date": str(end_date), "source": "fallback"},
            )

        task_dates[tm_id] = {"start": None, "end": end_date}

    # --- ASAP Scheduling Logic ---
    # Process tasks in a topological order if possible, or iteratively until stable
    # For simplicity, we'll iterate a few times to propagate dates
    # A more robust solution would use a proper topological sort or critical path algorithm.

    # Sort tasks by ID to ensure a somewhat consistent processing order,
    # though true topological sort is better for complex dependencies.
    sorted_tm_ids = sorted(
        taskmaster_tasks.keys(),
        key=lambda x: [int(i) if i.isdigit() else i for i in x.split(".")],
    )

    # Iterate multiple times to ensure all dependencies are resolved
    for _ in range(len(sorted_tm_ids) * 2):  # Iterate enough times to propagate changes
        for tm_id in sorted_tm_ids:
            tm_task = taskmaster_tasks[tm_id]
            current_task_info = task_dates[tm_id]
            issue = task_id_to_issue.get(tm_id)

            # --- 1. Handle 'done' tasks: their dates are fixed and should not be changed by ASAP logic ---
            if tm_task.get("status") == "done":
                # Ensure start date is set based on created_at or inferred from closed_at
                if issue and issue.created_at:
                    current_task_info["start"] = datetime.fromisoformat(issue.created_at).date()
                else:
                    # Fallback if no created_at for done task
                    current_task_info["start"] = current_task_info["end"] - timedelta(days=1)

                # Ensure done task's start date is not pushed beyond its end date (closed_at)
                if current_task_info["start"] > current_task_info["end"]:
                    current_task_info["start"] = current_task_info["end"]
                    logger.warning(
                        operation="prepare_gantt_data",
                        message=f"Task {tm_id} (done): Adjusted start date to be <= end date: {current_task_info['start']}",
                        context={
                            "task_id": tm_id,
                            "start_date": str(current_task_info["start"]),
                            "end_date": str(current_task_info["end"]),
                        },
                    )
                # If start and end dates are the same for a done task, extend end date by 1 day for visibility
                if current_task_info["start"] == current_task_info["end"]:
                    current_task_info["end"] = current_task_info["end"] + timedelta(days=1)
                    logger.debug(
                        operation="prepare_gantt_data",
                        message=f"Task {tm_id} (done): End date adjusted by 1 day for visibility: {current_task_info['end']}",
                        context={
                            "task_id": tm_id,
                            "start_date": str(current_task_info["start"]),
                            "end_date": str(current_task_info["end"]),
                        },
                    )
                continue  # Skip further ASAP logic for done tasks

            # --- 2. Handle non-done tasks: apply ASAP logic ---
            earliest_possible_start = None

            # Prioritize task's own created_at if it's an independent task
            if not tm_task.get("dependencies") and issue and issue.created_at:
                task_created_at = datetime.fromisoformat(issue.created_at).date()
                if overall_start_date:
                    # Use the later of task_created_at and overall_start_date
                    earliest_possible_start = max(task_created_at, overall_start_date)
                else:
                    earliest_possible_start = task_created_at

            # If dependencies exist, or no specific created_at for independent task, calculate based on dependencies
            if tm_task.get("dependencies"):
                max_dep_end_date = None
                for dep_id in tm_task["dependencies"]:
                    dep_dates = task_dates.get(str(dep_id))
                    if dep_dates and dep_dates["end"]:
                        if max_dep_end_date is None or dep_dates["end"] > max_dep_end_date:
                            max_dep_end_date = dep_dates["end"]

                if max_dep_end_date:
                    dep_based_start = get_next_working_day(max_dep_end_date, country_holidays)
                    if earliest_possible_start is None or dep_based_start > earliest_possible_start:
                        earliest_possible_start = dep_based_start

            # Fallback if no dependencies and no specific created_at, or if dependencies result in earlier date
            if earliest_possible_start is None:
                if overall_start_date:
                    earliest_possible_start = overall_start_date
                elif earliest_created_at:
                    earliest_possible_start = earliest_created_at
                else:
                    earliest_possible_start = today  # Final Fallback

            # Update current task's start date
            if current_task_info["start"] is None or earliest_possible_start > current_task_info["start"]:
                current_task_info["start"] = earliest_possible_start
                logger.debug(
                    operation="prepare_gantt_data",
                    message=f"Task {tm_id}: Start date set to {current_task_info['start']}",
                    context={"task_id": tm_id, "start_date": str(current_task_info["start"])},
                )

            # Ensure end_date is not before start_date (minimum 1 day duration)
            if current_task_info["end"] < current_task_info["start"]:
                current_task_info["end"] = current_task_info["start"] + timedelta(days=1)
                logger.warning(
                    operation="prepare_gantt_data",
                    message=f"Task {tm_id}: End date adjusted to {current_task_info['end']} to be >= start date.",
                    context={
                        "task_id": tm_id,
                        "start_date": str(current_task_info["start"]),
                        "end_date": str(current_task_info["end"]),
                    },
                )
            elif current_task_info["end"] == current_task_info["start"]:
                current_task_info["end"] = current_task_info["start"] + timedelta(days=1)
                logger.debug(
                    operation="prepare_gantt_data",
                    message=f"Task {tm_id}: End date adjusted by 1 day as start and end were same.",
                    context={
                        "task_id": tm_id,
                        "start_date": str(current_task_info["start"]),
                        "end_date": str(current_task_info["end"]),
                    },
                )

    # Final pass: build DataFrame
    for tm_id, tm_task in taskmaster_tasks.items():
        start_date = task_dates[tm_id]["start"]
        end_date = task_dates[tm_id]["end"]

        if start_date is None:
            logger.warning(
                operation="prepare_gantt_data",
                message=f"Task {tm_id}: Start date could not be determined. Using today.",
                context={"task_id": tm_id},
            )
            start_date = today
        if end_date is None:
            logger.warning(
                operation="prepare_gantt_data",
                message=f"Task {tm_id}: End date could not be determined. Using start_date + 7 days.",
                context={"task_id": tm_id, "start_date": str(start_date)},
            )
            end_date = start_date + timedelta(days=7)

        status = tm_task.get("status", "unknown")
        color_map = {
            "done": "#28a745",
            "in-progress": "#fd7e14",
            "pending": "#007bff",
            "blocked": "#dc3545",
            "unknown": "#6c757d",
        }

        df_data.append(
            dict(
                Task=f"{tm_id}: {tm_task['title']}",
                Start=start_date,
                Finish=end_date,
                Status=status,
                Color=color_map.get(status, "#6c757d"),
                TaskID=tm_id,  # Add TaskID for dependency drawing
            )
        )
        logger.debug(
            operation="prepare_gantt_data",
            message=f"Task {tm_id}: Start={start_date}, End={end_date}, Status={status}",
            context={"task_id": tm_id, "start": str(start_date), "end": str(end_date), "status": status},
        )

        # Subtasks from description
        issue = task_id_to_issue.get(tm_id)
        if issue and issue.description:
            sub_tasks_from_desc = parse_task_list(issue.description)
            for i, sub_t in enumerate(sub_tasks_from_desc):
                sub_task_name = f"{tm_id}.{i+1}: {sub_t['title']}"
                sub_task_status = "done" if sub_t["completed"] else "pending"
                sub_task_color = "#A0A0A0"  # Subtasks are gray

                df_data.append(
                    dict(
                        Task=sub_task_name,
                        Start=start_date,  # Simplified: same duration as parent
                        Finish=end_date,  # Simplified: same duration as parent
                        Status=sub_task_status,
                        Color=sub_task_color,
                        TaskID=f"{tm_id}.{i+1}",  # Add TaskID for subtasks
                    )
                )
                logger.debug(
                    operation="prepare_gantt_data",
                    message=f"Subtask {sub_task_name}: Start={start_date}, End={end_date}, Status={sub_task_status}",
                    context={
                        "subtask_name": sub_task_name,
                        "start": str(start_date),
                        "end": str(end_date),
                        "status": sub_task_status,
                    },
                )

    logger.info(
        operation="prepare_gantt_data",
        message=f"Prepared {len(df_data)} entries for Gantt chart.",
        context={"num_entries": len(df_data)},
    )
    return pd.DataFrame(df_data)


# --- Chart Generation Module ---


def generate_gantt_chart(
    df,
    output_path,
    taskmaster_tasks,
    dry_run,
    output_format,
    project_name,
    country_holidays,
):
    """Generates and saves the Gantt chart HTML file using Plotly."""
    if df.empty:
        logger.warning(operation="generate_gantt_chart", message="DataFrame is empty. Cannot generate chart.")
        return

    fig = px.timeline(
        df,
        x_start="Start",
        x_end="Finish",
        y="Task",
        color="Status",
        color_discrete_map={
            "done": "#28a745",
            "in-progress": "#fd7e14",
            "pending": "#007bff",
            "blocked": "#dc3545",
            "unknown": "#6c757d",
        },
        title=f"{project_name} Gantt Chart",  # Use project_name here
        labels={"Task": "Tasks"},
    )

    fig.update_yaxes(autorange="reversed")

    # Add non-working day shapes
    start_of_chart = df["Start"].min() - timedelta(days=7)
    end_of_chart = df["Finish"].max() + timedelta(days=7)
    shapes = []
    current_day = start_of_chart
    while current_day <= end_of_chart:
        if not is_working_day(current_day, country_holidays):
            shapes.append(
                go.layout.Shape(
                    type="rect",
                    xref="x",
                    yref="paper",
                    x0=current_day,
                    y0=0,
                    x1=current_day + timedelta(days=1),
                    y1=1,
                    fillcolor="rgba(0,0,0,0.05)",
                    layer="below",
                    line_width=0,
                )
            )
        current_day += timedelta(days=1)

    fig.update_layout(shapes=shapes)

    # --- Dependency Arrow Drawing Logic (PoC Revisit) ---
    # Get the actual order of tasks on the y-axis after Plotly renders them
    # This is crucial because Plotly might reorder tasks based on various factors.
    # We need to map TaskID to its numerical Y-axis position
    # The y-axis labels are in the format "TaskID: Task Title"
    # So, we extract TaskID from the ticktext
    y_axis_task_labels = fig.layout.yaxis.ticktext

    annotations = []
    if y_axis_task_labels is None or not y_axis_task_labels:  # Added check
        logger.warning(
            operation="generate_gantt_chart", message="No y-axis tick labels found. Skipping dependency arrow drawing."
        )
    else:
        y_axis_task_ids = [label.split(": ", 1)[0] for label in y_axis_task_labels]
        y_axis_position_map = {task_id: i for i, task_id in enumerate(y_axis_task_ids)}

        for tm_id, tm_task in taskmaster_tasks.items():
            if tm_task.get("dependencies"):
                current_task_df_row = df[df["TaskID"] == tm_id]
                if current_task_df_row.empty:
                    logger.warning(
                        operation="generate_gantt_chart",
                        message=f"Current task {tm_id} not found in DataFrame for dependency drawing.",
                        context={"task_id": tm_id},
                    )
                    continue
                current_task_df_row = current_task_df_row.iloc[0]

                for dep_id in tm_task["dependencies"]:
                    dep_task_df_row = df[df["TaskID"] == str(dep_id)]
                    if dep_task_df_row.empty:
                        logger.warning(
                            operation="generate_gantt_chart",
                            message=f"Dependent task {dep_id} not found in DataFrame for dependency drawing.",
                            context={"dependency_id": dep_id},
                        )
                        continue
                    dep_task_df_row = dep_task_df_row.iloc[0]

                    # X-coordinates: from end of dependent task to start of current task
                    x_start_arrow = dep_task_df_row["Finish"]
                    x_end_arrow = current_task_df_row["Start"]

                    # Y-coordinates: based on their position in the chart
                    y_start_pos = y_axis_position_map.get(str(dep_id))
                    y_end_pos = y_axis_position_map.get(tm_id)

                    if y_start_pos is not None and y_end_pos is not None:
                        # Adjust Y-position to be in the middle of the bar
                        # Plotly's y-axis is categorical, so yref='y' uses category index
                        # For arrows, we need to use 'y' for the actual position on the axis
                        # The y-axis range is from -0.5 to N-0.5 for N categories
                        # So, category 'i' is at y=i
                        y_start_arrow = y_start_pos  # + 0.0 # Center of the bar
                        y_end_arrow = y_end_pos  # + 0.0 # Center of the bar

                        # Add a slight offset if tasks are on the same row to avoid overlap
                        y_offset = 0.1  # Small offset for visual clarity
                        if y_start_arrow == y_end_arrow:
                            y_end_arrow += y_offset  # Shift end of arrow slightly down

                        annotations.append(
                            go.layout.Annotation(
                                x=x_end_arrow,  # Arrow tip x
                                y=y_end_arrow,  # Arrow tip y
                                xref="x",
                                yref="y",
                                ax=x_start_arrow,  # Arrow tail x
                                ay=y_start_arrow,  # Arrow tail y
                                axref="x",
                                ayref="y",
                                showarrow=True,
                                arrowhead=2,
                                arrowsize=1,
                                arrowwidth=1,
                                arrowcolor="#000000",
                                standoff=0,
                                startstandoff=0,
                            )
                        )
    fig.update_layout(annotations=annotations)

    if not dry_run:
        if output_format == "html":
            fig.write_html(str(output_path))
            logger.info(
                operation="generate_gantt_chart",
                message=f"Gantt chart saved to: {output_path}",
                context={"output_path": str(output_path), "format": "html"},
            )
        elif output_format in ["png", "jpeg", "webp", "svg", "pdf"]:
            try:
                fig.write_image(str(output_path))
                logger.info(
                    operation="generate_gantt_chart",
                    message=f"Gantt chart saved to: {output_path}",
                    context={"output_path": str(output_path), "format": output_format},
                )
            except Exception as e:
                logger.error(
                    operation="generate_gantt_chart",
                    message=f"Failed to save image to {output_path}. Ensure kaleido is installed: {e}",
                    context={"output_path": str(output_path), "error": str(e)},
                )
        else:
            logger.error(
                operation="generate_gantt_chart",
                message=f"Unsupported output format: {output_format}",
                context={"format": output_format},
            )
    else:
        logger.info(
            operation="generate_gantt_chart",
            message=f"Dry run: Gantt chart would have been saved to: {output_path} (format: {output_format})",
            context={"output_path": str(output_path), "format": output_format, "dry_run": True},
        )


# --- Main Execution Flow ---


def main():
    """Main function to run the Gantt chart generator."""
    parser = argparse.ArgumentParser(description="Generate an interactive Gantt chart from Taskmaster and GitLab data.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the chart generation without saving the HTML file.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(Path("/workspace/gantt_chart.html")),
        help="Specify the output HTML file path.",
    )
    parser.add_argument(
        "--format",
        type=str,
        default="html",
        choices=["html", "png", "jpeg", "webp", "svg", "pdf"],
        help="Specify the output format (html, png, jpeg, webp, svg, pdf). Requires kaleido for image formats.",
    )
    args = parser.parse_args()

    # VibeLogger handles its own configuration and output.
    # We can set the minimum logging level for VibeLogger if needed,
    # but for now, we'll let its internal config manage it.
    # For example, to set a minimum level:
    # vibe_config.min_level = args.log_level.upper()

    logger.info(operation="main", message="--- Starting Gantt Chart Generator ---")
    if args.dry_run:
        logger.info(operation="main", message="Running in DRY RUN mode. No HTML file will be saved.", context={"dry_run": True})

    # 1. Load settings
    config = dotenv_values()
    gitlab_base_url = config.get("GITLAB_BASE_URL")
    gitlab_token = config.get("GITLAB_PERSONAL_ACCESS_TOKEN")
    project_id = config.get("GITLAB_PROJECT_ID")
    gantt_start_date_str = config.get("GANTT_START_DATE")
    holiday_country = config.get("HOLIDAY_COUNTRY", "JP")  # Default to Japan

    try:
        country_holidays = holidays.CountryHoliday(holiday_country, years=date.today().year)
        logger.info(operation="main", message=f"Using holidays for {holiday_country}.", context={"country": holiday_country})
    except KeyError:
        logger.warning(
            operation="main",
            message=f"Holiday country '{holiday_country}' not found. No holidays will be observed.",
            context={"country": holiday_country},
        )
        country_holidays = {}

    gitlab_ssl_verify = True
    ssl_verify_str = config.get("GITLAB_SSL_VERIFY", "true")
    if ssl_verify_str.lower() in ("false", "0", "no"):
        gitlab_ssl_verify = False
    elif ssl_verify_str.lower() in ("true", "1", "yes"):
        gitlab_ssl_verify = True
    else:
        gitlab_ssl_verify = ssl_verify_str

    if not all([gitlab_base_url, gitlab_token, project_id]):  # Changed from gitlab_url
        logger.critical(operation="main", message="Missing GitLab configuration in .env file. Aborting.")
        sys.exit(1)

    # 2. Connect to GitLab
    gl = None
    try:
        gl = gitlab.Gitlab(gitlab_base_url, private_token=gitlab_token, ssl_verify=gitlab_ssl_verify)  # Changed from gitlab_url
        gl.auth()
        logger.info(operation="main", message="Successfully authenticated with GitLab.")
    except Exception as e:
        logger.critical(operation="main", message=f"Failed to connect to GitLab: {e}. Aborting.", context={"error": str(e)})
        sys.exit(1)

    # 3. Process data
    overall_start_date = None
    if gantt_start_date_str:
        try:
            overall_start_date = datetime.strptime(gantt_start_date_str, "%Y-%m-%d").date()
            logger.info(
                operation="main",
                message=f"Overall Gantt start date from .env: {overall_start_date}",
                context={"start_date": str(overall_start_date)},
            )
        except ValueError:
            logger.warning(
                operation="main",
                message=f"Invalid GANTT_START_DATE format in .env: {gantt_start_date_str}. Ignoring.",
                context={"gantt_start_date_str": gantt_start_date_str},
            )

    taskmaster_tasks = load_taskmaster_tasks()
    if not taskmaster_tasks:
        logger.critical(operation="main", message="No Taskmaster tasks loaded. Aborting.")
        sys.exit(1)

    gitlab_issues, project_name = get_gitlab_issues(gl, project_id)  # Get project_name here
    if not gitlab_issues:
        logger.warning(operation="main", message="No GitLab issues fetched. Chart might be incomplete.")

    task_id_to_issue = map_tasks_and_issues(gitlab_issues)

    df = prepare_gantt_data(taskmaster_tasks, task_id_to_issue, overall_start_date, country_holidays)

    # 4. Generate chart
    generate_gantt_chart(
        df,
        Path(args.output),
        taskmaster_tasks,
        args.dry_run,
        args.format,
        project_name,
        country_holidays,
    )

    logger.info(operation="main", message="--- Gantt Chart Generation Finished ---")


if __name__ == "__main__":
    main()
