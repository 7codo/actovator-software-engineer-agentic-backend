"""
Notes:
- add-repository doesn't work even with root permission so I use curl instead
"""

from e2b import Template, default_build_logger, wait_for_url
from app.constants import PROJECT_PATH
from app.core.config import settings

template = (
    Template()
    .from_template("mcp-gateway")
    .set_workdir("/home/user")
    # 1. become root, install curl, build Python 3.13
    .run_cmd(
        (
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
        ),
        user="root",
    )  # <── run as root
    .run_cmd(
        [
            "npm install -g npm@latest",
            "pip install uv",
            "npm install -g pm2",
        ],
        user="root",
    )
    .run_cmd(
        [
            "npx playwright install chromium",
            "npx playwright install-deps chromium",
            "npm install -g agent-browser",
            "agent-browser install",
            "agent-browser install --with-deps",
        ],
        user="root",
    )
    .set_user("user")
    .set_workdir(PROJECT_PATH)
    .run_cmd(
        'npx create-next-app . --ts --tailwind --eslint --import-alias "@/*" '
        "--use-npm --app --no-react-compiler --src-dir --turbopack"
    )
    .run_cmd(
        'mkdir -p /home/user/project/.actovator && echo \'{"languages": ["bash", "markdown", "toml", "typescript", "yaml"]}\' > /home/user/project/.actovator/config.json'
    )  # create .actovator then initia languages fit nextjs project
    .run_cmd("npx shadcn@latest init -d")
    .run_cmd("npx shadcn@latest add button")
    .set_workdir(PROJECT_PATH)
    .set_start_cmd(
        'pm2 start npm --name "project" -- run dev',
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
