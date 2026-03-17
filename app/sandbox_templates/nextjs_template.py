"""
Notes:
- add-repository doesn't work even with root permission so I use curl instead
- additionally, we clone the public repo https://github.com/7codo/serena to /home/user
- then, we run the FastAPI app using: uv run serena-server --project PROJECT_PATH
"""

from e2b import Template, default_build_logger, wait_for_url, wait_for_port
from app.constants import PROJECT_PATH
from app.core.config import settings


def install_python_313_cmd():
    return (
        "apt-get update "
        "&& apt-get install -y curl build-essential libssl-dev zlib1g-dev "
        "libbz2-dev libreadline-dev libsqlite3-dev libncursesw5-dev xz-utils "
        "tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev && "
        "curl -O https://www.python.org/ftp/python/3.13.0/Python-3.13.0.tgz && "
        "tar xzf Python-3.13.0.tgz && cd Python-3.13.0 && "
        "./configure --enable-optimizations --prefix=/usr/local && "
        "make -j$(nproc) && make altinstall && "
        "curl -sS https://bootstrap.pypa.io/get-pip.py | python3.13 && "
        "cd /home/user && rm -rf Python-3.13.0 Python-3.13.0.tgz"
    )


def install_github_cli_cmd():
    return (
        "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && "
        'echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && '
        "apt update && "
        "apt install gh -y"
    )


def install_global_tools_cmds():
    return [
        "npm install -g npm@latest",
        "pip install uv",
        "npm install -g pm2",
    ]


def install_playwright_and_agent_browser_cmds():
    return [
        "npx playwright install chromium",
        "npx playwright install-deps chromium",
        "npm install -g agent-browser",
        "agent-browser install",
        "agent-browser install --with-deps",
        (
            "apt-get install -y "
            "libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libatk1.0-0 libatk-bridge2.0-0 "
            "libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 "
            "libgbm1 libasound2 libnspr4 libnss3 libx11-6 libxcb1 libxext6 libxss1 libxtst6 "
            "fonts-liberation libappindicator3-1 libu2f-udev libvulkan1"
        ),  # for Chromium
    ]


def init_actovator_cmd():
    return (
        "mkdir -p .actovator && "
        'echo \'{"languages": ["bash", "markdown", "toml", "typescript", "yaml"]}\' > .actovator/config.json'
    )


def create_test_directories_cmd():
    return "mkdir -p .actovator/bashs .actovator/tests/e2e .actovator/tests/e2e/screenshots .actovator/features"


def set_shadcn_init_cmds():
    return ["npx shadcn@latest init -d", "npx shadcn@latest add button"]


def run_init_next_script_cmd():
    return [
        "chmod +x .actovator/init-next.sh && .actovator/init-next.sh",
        "rm .actovator/init-next.sh",
    ]


def write_tech_stack_json_cmd():
    # Create the features dir if missing and write the tech stack file.
    tech_stack_json = '{\n  "ecosystem": "nextjs, typescript, tailwindcss, shadcn"\n}'
    return (
        "mkdir -p .actovator/features && "
        f"echo '{tech_stack_json}' > .actovator/features/tech_stack.json"
    )


def clone_serena_repo_cmd():
    # Shallow clone for faster CI image builds
    return "git clone --depth=1 https://github.com/7codo/serena.git /home/user/serena"


template = (
    Template()
    .from_template("mcp-gateway")
    .set_workdir("/home/user")
    .run_cmd(install_python_313_cmd(), user="root")
    .run_cmd(install_github_cli_cmd(), user="root")
    .run_cmd(install_global_tools_cmds(), user="root")
    .run_cmd(install_playwright_and_agent_browser_cmds(), user="root")
    .run_cmd(clone_serena_repo_cmd())
    .set_user("user")
    .set_workdir(PROJECT_PATH)
    .run_cmd(
        'npx create-next-app . --ts --tailwind --eslint --import-alias "@/*" '
        "--use-npm --app --no-react-compiler --src-dir --turbopack"
    )
    .run_cmd(init_actovator_cmd())
    .run_cmd(create_test_directories_cmd())
    .run_cmd(set_shadcn_init_cmds())
    .copy("nextjs_cleanup_script.sh", ".actovator/init-next.sh")
    .run_cmd(run_init_next_script_cmd())
    .run_cmd(write_tech_stack_json_cmd())
.set_start_cmd(
    f'pm2 start npm --name "project" -- run dev ; '
    f'pm2 start uv --name "serena" -- run --directory /home/user/serena serena-server --project {PROJECT_PATH}',
    wait_for_url("http://localhost:3000"),
)
)

Template.build(
    template,
    alias="nextjs-latest",
    cpu_count=4,
    memory_mb=4096,
    on_build_logs=default_build_logger(),
    api_key=settings.e2b_api_key,
    # skip_cache=True,
)
