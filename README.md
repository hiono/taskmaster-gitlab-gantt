# Taskmaster GitLab Gantt

Generate interactive Gantt charts from [Taskmaster](https://www.task-master.dev/) and [GitLab](https://about.gitlab.com/) data.

## Relationship with `taskmaster-gitlab-sync`

This tool (`hiono/taskmaster-gitlab-gantt`) works in conjunction with the `hiono/taskmaster-gitlab-sync` CLI tool. The `taskmaster-gitlab-sync` tool is responsible for synchronizing your local Taskmaster tasks with GitLab issues, ensuring that GitLab acts as a reflection of your Taskmaster data.

`taskmaster-gitlab-gantt` then utilizes this synchronized data from GitLab (issues, their dates, and dependencies) and your local Taskmaster tasks to generate comprehensive Gantt charts. Therefore, it is recommended to run `taskmaster-gitlab-sync` regularly to ensure the Gantt chart reflects the most up-to-date project status.

## Overview

This tool helps visualize project progress by integrating tasks from Taskmaster and issues from GitLab into a single, interactive Gantt chart. The chart is generated as an HTML file, which can be easily shared or embedded.

## Features

- Data Integration: Combines task data from Taskmaster (`.taskmaster/tasks/tasks.json`) and issue data from GitLab.
- Flexible Date Calculation: Dynamically determines task start and end dates using an **ASAP (As Soon As Possible) scheduling** approach. It prioritizes:
    - Actual `closed_at` and `created_at` for completed tasks.
    - Dependencies for dependent tasks.
    - Task's own `created_at` for independent tasks, considering the `GANTT_START_DATE` if set (adopting the later of the two).
- Dependency Handling: Calculates task dependencies based on Taskmaster data.
- Non-Working Day Support: Considers weekends and Japanese national holidays for accurate scheduling.
- Status-based Coloring: Visualizes task status (done, in-progress, pending, blocked) with distinct colors.
- Multiple Output Formats: Generates charts in HTML (interactive), PNG, SVG, JPEG, WEBP, and PDF formats.
- Logging: Provides detailed logging for better understanding of the generation process.

## Installation

1. Clone the repository:

    ```bash
    git clone https://github.com/your-username/taskmaster-gitlab-gantt.git
    cd taskmaster-gitlab-gantt
    ```

2. Install dependencies:

    It's recommended to use `uv` for dependency management and virtual environment creation.

    ```bash
    uv venv
    uv pip install .
    ```

    Alternatively, using `pip`:

    ```bash
    python -m venv .venv
    source .venv/bin/activate
    pip install .
    ```

## Configuration

Create a `.env` file in the root directory of the project with the following content:

```env
# GitLab Configuration
GITLAB_BASE_URL="https://your-gitlab-instance.com" # Your GitLab instance base URL (e.g., https://gitlab.com or https://your-instance.com/git)
GITLAB_PERSONAL_ACCESS_TOKEN="your_personal_access_token" # Your GitLab Personal Access Token with 'api' scope
GITLAB_PROJECT_ID="12345" # The ID of your GitLab project (e.g., 12345)
GITLAB_SSL_VERIFY="true" # Set to 'true', 'false', or path to CA_BUNDLE file (e.g., '/path/to/ca.pem')

# Optional: Overall Gantt Chart Start Date
GANTT_START_DATE="YYYY-MM-DD" # Optional: e.g., "2025-01-01". If not set, derived from task data.

# Optional: Country for holiday calculation
HOLIDAY_COUNTRY="JP" # Optional: e.g., "US", "GB", "FR", "CN". Defaults to "JP" (Japan).
```

- `GITLAB_BASE_URL`: The base URL of your GitLab instance. Do not include `/api/v4` at the end.
- `GITLAB_PERSONAL_ACCESS_TOKEN`: A GitLab Personal Access Token with `api` scope. You can generate one from `User Settings > Access Tokens`.
- `GITLAB_PROJECT_ID`: The numerical ID of the GitLab project you want to generate the Gantt chart for.
- `GITLAB_SSL_VERIFY`: Controls SSL certificate verification. Set to `"false"` to disable verification (not recommended for production), or provide a path to a CA bundle file.
- `GANTT_START_DATE`: An optional overall start date for the Gantt chart in `YYYY-MM-DD` format. If not set, task start dates are derived from `created_at` or dependency logic. When set, it acts as a minimum start date for non-completed tasks without dependencies, effectively shifting their start date to `GANTT_START_DATE` if their `created_at` is earlier.

**Important**: Add your `.env` file to `.gitignore` to prevent it from being committed to your repository.

## Usage

After installation and configuration, you can run the tool using the `tmgantt` command:

```bash
tmgantt [OPTIONS]
```

There are two primary ways to run the `tmgantt` command:

1.  After activating the virtual environment:
    If you have activated the virtual environment (e.g., by running `source .venv/bin/activate`), you can directly use the `tmgantt` command. This makes the `tmgantt` script available in your shell's PATH.

    ```bash
    source .venv/bin/activate
    tmgantt --output my_gantt.html
    ```

2.  Using `uv run` (recommended for convenience):
    The `uv run` command allows you to execute `tmgantt` without explicitly activating the virtual environment. `uv` automatically handles finding and running the script within the project's virtual environment. This is often more convenient, especially for one-off commands or in automated scripts.

    ```bash
    uv run tmgantt --output my_gantt.html
    ```

### Options

- `--output <PATH>`: Specify the output HTML file path (default: `gantt_chart.html`).
- `--format <FORMAT>`: Specify the output format (e.g., `html`, `png`, `svg`, `pdf`). Requires `kaleido` for image formats. (default: `html`)
- `--dry-run`: Simulate the chart generation without saving the output file. Useful for checking data processing and logging.
- `--log-level <LEVEL>`: Set the logging level (e.g., `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). (default: `INFO`)

### Examples

- Generate an HTML Gantt chart:

    ```bash
    tmgantt
    ```

- Generate a PNG image of the Gantt chart:

    ```bash
    tmgantt --format png --output my_gantt_chart.png
    ```

- Run in dry-run mode with debug logging:

    ```bash
    tmgantt --dry-run --log-level DEBUG
    ```

## Development

### Code Formatting

This project uses `black` and `isort` for code formatting. The line length is set to 128 characters.

To format the code, run:

```bash
uvx black . && uvx isort .
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
