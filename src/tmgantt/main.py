"""
Gantt Chart Generator from Taskmaster & GitLab

This script generates an interactive Gantt chart in HTML format based on data from
Taskmaster tasks and GitLab issues.

Configuration is loaded from a `.env` file (GitLab base URL, personal access token, project ID, SSL verification setting, and optional overall Gantt start date).

Refer to `taskmaster-gitlab-gantt-spec.md` for detailed specifications.
"""

import argparse
import json
import logging
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

# --- Constants & Settings ---
# TASKS_PATH = Path("/workspace/.taskmaster/tasks/tasks.json") # Moved to main
# DEFAULT_OUTPUT_HTML_PATH = Path("/workspace/gantt_chart.html") # Moved to main

# --- Logger Setup ---
logger = logging.getLogger(__name__)


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
            logger.info(f"Successfully loaded {len(all_tasks)} tasks from Taskmaster.")
        else:
            logger.warning(f"Tag '{tag}' not found in {tasks_path}. No tasks will be loaded.")
    except FileNotFoundError:
        logger.error(f"Taskmaster file not found at {tasks_path}")
    except json.JSONDecodeError:
        logger.error(f"Failed to parse Taskmaster JSON file: {tasks_path}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while loading Taskmaster tasks: {e}")
    return all_tasks


def get_gitlab_issues(gl, project_id):
    """Fetches all issues from the specified GitLab project."""
    issues = []
    project_name = "Unknown Project"
    try:
        project = gl.projects.get(project_id)
        project_name = project.name_with_namespace  # Get project name
        logger.info(f"Accessing GitLab project: '{project_name}'")
        issues = project.issues.list(all=True)
        logger.info(f"Successfully fetched {len(issues)} issues from GitLab.")
    except gitlab.exceptions.GitlabError as e:
        logger.error(f"GitLab API error while fetching issues: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while fetching GitLab issues: {e}")
    return issues, project_name  # Return project_name as well


def map_tasks_and_issues(gitlab_issues):
    """Creates a mapping from Taskmaster task ID to GitLab issue object."""
    mapping = {}
    for issue in gitlab_issues:
        match = re.match(r"^([0-9\.]+):", issue.title)
        if match:
            mapping[match.group(1)] = issue
    logger.debug(f"Mapped {len(mapping)} GitLab issues to Taskmaster IDs.")
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
    """Prepares and processes data into a pandas DataFrame for Plotly."""
    df_data = []
    today = datetime.now().date()

    # First pass: determine dates for all tasks
    task_dates = {}
    for tm_id, tm_task in taskmaster_tasks.items():
        issue = task_id_to_issue.get(tm_id)
        start_date, end_date = None, None

        # Determine end_date
        if tm_task.get("status") == "done" and issue and issue.closed_at:
            end_date = datetime.fromisoformat(issue.closed_at).date()
            logger.debug(f"Task {tm_id}: Using closed_at for end_date: {end_date}")
        elif issue and issue.due_date:
            end_date = datetime.strptime(issue.due_date, "%Y-%m-%d").date()
            logger.debug(f"Task {tm_id}: Using due_date for end_date: {end_date}")
        else:
            end_date = today + timedelta(days=7)  # Fallback
            logger.debug(f"Task {tm_id}: Using fallback end_date: {end_date}")

        # Auto-extend delayed tasks
        if tm_task.get("status") != "done" and end_date < today:
            logger.info(f"Task '{tm_id}' is delayed. Extending end_date to today.")
            end_date = today

        task_dates[tm_id] = {"start": None, "end": end_date}

    # Second pass: determine start_dates based on dependencies
    for tm_id, tm_task in taskmaster_tasks.items():
        if not tm_task.get("dependencies"):
            # Priority A: overall_start_date from .env
            if overall_start_date:
                task_dates[tm_id]["start"] = overall_start_date
                logger.debug(f"Task {tm_id}: Start date from overall_start_date: {overall_start_date}")
            else:
                # Priority B: created_at from GitLab Issue
                issue = task_id_to_issue.get(tm_id)
                if issue and issue.created_at:
                    task_dates[tm_id]["start"] = datetime.fromisoformat(issue.created_at).date()
                    logger.debug(f"Task {tm_id}: Start date from created_at: {task_dates[tm_id]['start']}")
                else:
                    # Fallback: today
                    task_dates[tm_id]["start"] = today
                    logger.debug(f"Task {tm_id}: Start date from fallback today: {today}")
        else:
            # Dependent tasks: next working day after max dependency end date
            max_dep_end_date = None
            for dep_id in tm_task["dependencies"]:
                dep_dates = task_dates.get(str(dep_id))
                if dep_dates and dep_dates["end"]:
                    if max_dep_end_date is None or dep_dates["end"] > max_dep_end_date:
                        max_dep_end_date = dep_dates["end"]

            if max_dep_end_date:
                task_dates[tm_id]["start"] = get_next_working_day(max_dep_end_date, country_holidays)
                logger.debug(f"Task {tm_id}: Start date from dependency: {task_dates[tm_id]['start']}")
            else:  # Fallback for dependencies if no valid dependency end date found
                task_dates[tm_id]["start"] = overall_start_date if overall_start_date else today
                logger.warning(
                    f"Task {tm_id}: No valid dependency end date found. Using fallback start date: {task_dates[tm_id]['start']}"
                )

    # Final pass: build DataFrame and adjust dates
    for tm_id, tm_task in taskmaster_tasks.items():
        start_date = task_dates[tm_id]["start"]
        end_date = task_dates[tm_id]["end"]

        if start_date is None:
            logger.warning(f"Task {tm_id}: Start date could not be determined. Using today.")
            start_date = today
        if end_date is None:
            logger.warning(f"Task {tm_id}: End date could not be determined. Using start_date + 7 days.")
            end_date = start_date + timedelta(days=7)

        # 完了済みタスクの場合、開始日が終了日より後になる場合は開始日を終了日に合わせる
        if tm_task.get("status") == "done" and start_date > end_date:
            logger.warning(
                f"Task {tm_id}: Done task's start date {start_date} is after its end date {end_date}. Adjusting start date to end date."
            )
            start_date = end_date

        if end_date < start_date:
            logger.warning(f"Task {tm_id}: End date {end_date} is before start date {start_date}. Adjusting end date.")
            end_date = start_date + timedelta(days=1)
        elif end_date == start_date:
            logger.debug(f"Task {tm_id}: Start and end dates are the same. Adjusting end date by 1 day.")
            end_date = start_date + timedelta(days=1)

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

    logger.info(f"Prepared {len(df_data)} entries for Gantt chart.")
    return pd.DataFrame(df_data)


# --- Chart Generation Module ---


def generate_gantt_chart(df, output_path, taskmaster_tasks, dry_run, output_format, project_name, country_holidays):
    """Generates and saves the Gantt chart HTML file using Plotly."""
    if df.empty:
        logger.warning("DataFrame is empty. Cannot generate chart.")
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
        logger.warning("No y-axis tick labels found. Skipping dependency arrow drawing.")
    else:
        y_axis_task_ids = [label.split(": ", 1)[0] for label in y_axis_task_labels]
        y_axis_position_map = {task_id: i for i, task_id in enumerate(y_axis_task_ids)}

        for tm_id, tm_task in taskmaster_tasks.items():
            if tm_task.get("dependencies"):
                current_task_df_row = df[df["TaskID"] == tm_id]
                if current_task_df_row.empty:
                    logger.warning(f"Current task {tm_id} not found in DataFrame for dependency drawing.")
                    continue
                current_task_df_row = current_task_df_row.iloc[0]

                for dep_id in tm_task["dependencies"]:
                    dep_task_df_row = df[df["TaskID"] == str(dep_id)]
                    if dep_task_df_row.empty:
                        logger.warning(f"Dependent task {dep_id} not found in DataFrame for dependency drawing.")
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
            logger.info(f"Gantt chart saved to: {output_path}")
        elif output_format in ["png", "jpeg", "webp", "svg", "pdf"]:
            try:
                fig.write_image(str(output_path))
                logger.info(f"Gantt chart saved to: {output_path}")
            except Exception as e:
                logger.error(f"Failed to save image to {output_path}. Ensure kaleido is installed: {e}")
        else:
            logger.error(f"Unsupported output format: {output_format}")
    else:
        logger.info(f"Dry run: Gantt chart would have been saved to: {output_path} (format: {output_format})")


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

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,  # Ensure logs go to stdout
    )

    logger.info("--- Starting Gantt Chart Generator ---")
    if args.dry_run:
        logger.info("Running in DRY RUN mode. No HTML file will be saved.")

    # 1. Load settings
    config = dotenv_values()
    gitlab_base_url = config.get("GITLAB_BASE_URL")
    gitlab_token = config.get("GITLAB_PERSONAL_ACCESS_TOKEN")
    project_id = config.get("GITLAB_PROJECT_ID")
    gantt_start_date_str = config.get("GANTT_START_DATE")
    holiday_country = config.get("HOLIDAY_COUNTRY", "JP")  # Default to Japan

    try:
        country_holidays = holidays.CountryHoliday(holiday_country, years=date.today().year)
        logger.info(f"Using holidays for {holiday_country}.")
    except KeyError:
        logger.warning(f"Holiday country '{holiday_country}' not found. No holidays will be observed.")
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
        logger.critical("Missing GitLab configuration in .env file. Aborting.")
        sys.exit(1)

    # 2. Connect to GitLab
    gl = None
    try:
        gl = gitlab.Gitlab(gitlab_base_url, private_token=gitlab_token, ssl_verify=gitlab_ssl_verify)  # Changed from gitlab_url
        gl.auth()
        logger.info("Successfully authenticated with GitLab.")
    except Exception as e:
        logger.critical(f"Failed to connect to GitLab: {e}. Aborting.")
        sys.exit(1)

    # 3. Process data
    overall_start_date = None
    if gantt_start_date_str:
        try:
            overall_start_date = datetime.strptime(gantt_start_date_str, "%Y-%m-%d").date()
            logger.info(f"Overall Gantt start date from .env: {overall_start_date}")
        except ValueError:
            logger.warning(f"Invalid GANTT_START_DATE format in .env: {gantt_start_date_str}. Ignoring.")

    taskmaster_tasks = load_taskmaster_tasks()
    if not taskmaster_tasks:
        logger.critical("No Taskmaster tasks loaded. Aborting.")
        sys.exit(1)

    gitlab_issues, project_name = get_gitlab_issues(gl, project_id)  # Get project_name here
    if not gitlab_issues:
        logger.warning("No GitLab issues fetched. Chart might be incomplete.")

    task_id_to_issue = map_tasks_and_issues(gitlab_issues)

    df = prepare_gantt_data(taskmaster_tasks, task_id_to_issue, overall_start_date, country_holidays)

    # 4. Generate chart
    generate_gantt_chart(df, Path(args.output), taskmaster_tasks, args.dry_run, args.format, project_name, country_holidays)

    logger.info("--- Gantt Chart Generation Finished ---")


if __name__ == "__main__":
    main()
