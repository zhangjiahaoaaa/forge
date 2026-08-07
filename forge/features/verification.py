"""PR 2: Acceptance Contract + External Verifier.

引入外部、可重复的验证决定 Task 是否完成。
核心语义：Agent final ≠ Task completed, Verifier PASS = Task completed.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

from ..core.runtime_secrets import secret_env_values
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


# ---------------------------------------------------------------------------
# 枚举类型
# ---------------------------------------------------------------------------

class VerifierVerdict(str, enum.Enum):
    """External Verifier 的确定性裁决结果。"""
    PASS = "pass"
    FAIL = "fail"
    INFRA_ERROR = "infra_error"
    FLAKY = "flaky"
    BLOCKED = "blocked"


class ReproductionVerdict(str, enum.Enum):
    """Reproduce 执行结果的分类。"""
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    INFRA_ERROR = "infra_error"
    FLAKY = "flaky"
    AMBIGUOUS = "ambiguous"


class ChangeScopeVerdict(str, enum.Enum):
    """文件变更范围检查的裁决。"""
    ALLOWED = "allowed"
    FORBIDDEN_MODIFIED = "forbidden_modified"
    ALLOWED_OUTSIDE_SCOPE = "allowed_outside_scope"


# ---------------------------------------------------------------------------
# Contract 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ReproduceExpectation:
    """定义 reproduce 命令应如何失败。"""
    outcome: str = "target_failure"
    exit_code: int | None = None
    failing_tests: list[str] = field(default_factory=list)
    output_contains: list[str] = field(default_factory=list)

    def matches(self, exit_code: int, stdout: str, stderr: str) -> bool:
        """判断实际输出是否符合预期失败特征。"""
        if self.outcome == "target_failure" and exit_code == 0:
            return False
        if self.exit_code is not None and exit_code != self.exit_code:
            return False
        combined = stdout + "\n" + stderr
        if self.failing_tests:
            if not all(t in combined for t in self.failing_tests):
                return False
        if self.output_contains:
            if not all(p in combined for p in self.output_contains):
                return False
        return True


@dataclass
class VerifyCommand:
    """验收阶段的一条检查命令。"""
    command: str
    timeout_seconds: int = 60


@dataclass
class ChangePolicy:
    """定义 Agent 允许和禁止的修改范围。"""
    allowed_paths: list[str] = field(default_factory=lambda: ["*"])
    forbidden_paths: list[str] = field(default_factory=list)


@dataclass
class AcceptanceContract:
    """由人定义的成功标准与约束。

    序列化为 contract.json，位于 .forge/tasks/<task_id>/contract.json。
    Agent 无权写入此路径，Runtime 在验证前会校验内容哈希。
    """
    schema_version: int = 1
    goal: str = ""
    reproduce: ReproduceExpectation | None = None
    reproduce_command: str = ""
    reproduce_timeout: int = 60
    verify_commands: list[VerifyCommand] = field(default_factory=list)
    change_policy: ChangePolicy = field(default_factory=ChangePolicy)
    default_contract: bool = False
    """是否由 `forge goal` 自动生成；未复现时不得据此判定任务已解决。"""

    @property
    def canonical_hash(self) -> str:
        """返回规范化 JSON 的 SHA-256，用于完整性校验。"""
        raw = json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @property
    def content_hash(self) -> str:
        """兼容旧接口：返回合约的规范化内容哈希。"""
        return self.canonical_hash

    def to_dict(self) -> dict:
        data = {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "reproduce": {
                "command": self.reproduce_command,
                "timeout_seconds": self.reproduce_timeout,
                "expect": {
                    "outcome": self.reproduce.outcome if self.reproduce else "target_failure",
                    "exit_code": self.reproduce.exit_code if self.reproduce else None,
                    "failing_tests": self.reproduce.failing_tests if self.reproduce else [],
                    "output_contains": self.reproduce.output_contains if self.reproduce else [],
                } if self.reproduce else None,
            } if self.reproduce_command else None,
            "verify": {
                "required": [
                    {"command": vc.command, "timeout_seconds": vc.timeout_seconds}
                    for vc in self.verify_commands
                ],
            } if self.verify_commands else None,
            "change_policy": {
                "allowed_paths": list(self.change_policy.allowed_paths),
                "forbidden_paths": list(self.change_policy.forbidden_paths),
            },
        }
        # 不向旧的显式 Contract 写入 false，保持已有冻结 hash 可继续校验。
        if self.default_contract:
            data["default_contract"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict) -> AcceptanceContract:
        data = data or {}

        # 支持两种格式：
        #   顶层 reproduce/verify (v0.1 实现)
        #   acceptance.reproduce / acceptance.verify (roadmap YAML 格式)
        acceptance = data.get("acceptance") or {}
        reproduce_data = acceptance.get("reproduce") if "reproduce" not in data else data.get("reproduce")
        if reproduce_data is None:
            reproduce_data = {}
        expect_data = reproduce_data.get("expect") or {} if isinstance(reproduce_data, dict) else {}

        reproduce = None
        if reproduce_data.get("command"):
            reproduce = ReproduceExpectation(
                outcome=str(expect_data.get("outcome", "target_failure")),
                exit_code=expect_data.get("exit_code"),
                failing_tests=list(expect_data.get("failing_tests", [])),
                output_contains=list(expect_data.get("output_contains", [])),
            )

        verify_data = acceptance.get("verify") if "verify" not in data else data.get("verify")
        if verify_data is None:
            verify_data = {}
        verify_commands = [
            VerifyCommand(command=str(v["command"]), timeout_seconds=int(v.get("timeout_seconds", 60)))
            for v in (verify_data.get("required") or [])
            if v.get("command")
        ]

        cp_data = data.get("change_policy") or {}
        change_policy = ChangePolicy(
            allowed_paths=list(cp_data.get("allowed_paths", ["*"])),
            forbidden_paths=list(cp_data.get("forbidden_paths", [])),
        )

        return cls(
            schema_version=int(data.get("schema_version", 1)),
            goal=str(data.get("goal", "")),
            reproduce=reproduce,
            reproduce_command=str(reproduce_data.get("command", "")),
            reproduce_timeout=int(reproduce_data.get("timeout_seconds", 60)),
            verify_commands=verify_commands,
            change_policy=change_policy,
            default_contract=bool(data.get("default_contract", False)),
        )


# ---------------------------------------------------------------------------
# 验证运行结果
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    """单条命令的完整执行结果（保存为证据 artifact）。"""
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timeout_seconds: int
    timed_out: bool = False
    started_at: str = ""
    finished_at: str = ""
    cwd: str = ""
    platform: str = ""
    python_version: str = ""

    def to_text(self) -> str:
        lines = [
            f"command: {self.command}",
            f"exit_code: {self.exit_code}",
            f"timeout_seconds: {self.timeout_seconds}",
            f"timed_out: {self.timed_out}",
            f"started_at: {self.started_at}",
            f"finished_at: {self.finished_at}",
            f"cwd: {self.cwd}",
            f"platform: {self.platform}",
            f"python_version: {self.python_version}",
            # JSON 编码保留换行及与分隔符相同的输出，供 from_text 无损恢复。
            f"command_json: {json.dumps(self.command, ensure_ascii=False)}",
            f"stdout_json: {json.dumps(self.stdout, ensure_ascii=False)}",
            f"stderr_json: {json.dumps(self.stderr, ensure_ascii=False)}",
            "--- stdout ---",
            self.stdout,
            "--- stderr ---",
            self.stderr,
        ]
        return "\n".join(lines)

    @classmethod
    def from_text(cls, text: str) -> CommandResult:
        lines = text.splitlines()
        command_json = _extract_line(lines, "command_json: ", "")
        stdout_json = _extract_line(lines, "stdout_json: ", "")
        stderr_json = _extract_line(lines, "stderr_json: ", "")
        return cls(
            command=json.loads(command_json) if command_json else _extract_line(lines, "command: ", ""),
            exit_code=int(_extract_line(lines, "exit_code: ", "0")),
            timeout_seconds=int(_extract_line(lines, "timeout_seconds: ", "60")),
            timed_out=_extract_line(lines, "timed_out: ", "False") == "True",
            started_at=_extract_line(lines, "started_at: ", ""),
            finished_at=_extract_line(lines, "finished_at: ", ""),
            cwd=_extract_line(lines, "cwd: ", ""),
            platform=_extract_line(lines, "platform: ", ""),
            python_version=_extract_line(lines, "python_version: ", ""),
            stdout=json.loads(stdout_json) if stdout_json else _extract_section(
                lines, "--- stdout ---", "--- stderr ---"
            ),
            stderr=json.loads(stderr_json) if stderr_json else _extract_after(lines, "--- stderr ---"),
        )


def _extract_line(lines: list[str], prefix: str, default: str) -> str:
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):]
    return default


def _extract_section(lines: list[str], start: str, end: str) -> str:
    in_section = False
    parts = []
    for line in lines:
        if line == start:
            in_section = True
            continue
        if line == end:
            break
        if in_section:
            parts.append(line)
    return "\n".join(parts)


def _extract_after(lines: list[str], marker: str) -> str:
    parts = []
    found = False
    for line in lines:
        if line == marker:
            found = True
            continue
        if found:
            parts.append(line)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 默认合同自动发现（forge goal）
# ---------------------------------------------------------------------------

def _find_test_files(workspace_root: Path) -> list[str]:
    """探测 pytest 测试文件（相对 POSIX 路径），排除运行时与依赖目录。"""
    ignored_parts = {".git", ".forge", ".pico", ".venv", "venv", "node_modules",
                     "__pycache__", ".pytest_cache", ".ruff_cache"}
    tests: list[str] = []
    root = Path(workspace_root).resolve()
    for pattern in ("test_*.py", "*_test.py"):
        for path in root.rglob(pattern):
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part in ignored_parts for part in rel.parts):
                continue
            tests.append(rel.as_posix())
    return sorted(set(tests))


def discover_default_contract(goal: str, workspace_root: Path) -> AcceptanceContract:
    """从目标自动生成一个可执行的默认合同（forge goal 用）。

    策略（规则化、确定性）：
    - 项目里有 pytest 测试 → 验收 = 跑全部测试通过；禁止修改测试文件
      （防模型改测试作弊）；复现 = 同一条命令，预期当前失败
      （若当前已通过，Loop 转人工，要求补充目标级验收）；
    - 没有测试 → 退化为"守门员合同"：验收 = 全部 .py 可编译（语法正确），
      允许改任何业务文件。

    这是"默认合同"，比手写合同粗糙；它的价值是让一句话目标可以直接开跑，
    验收标准依然客观可执行、模型依然无法自判完成。
    """
    root = Path(workspace_root).resolve()
    test_files = _find_test_files(root)

    if test_files:
        verify_command = "python -m pytest -q"
        change_policy = ChangePolicy(
            allowed_paths=["*"],
            forbidden_paths=list(test_files),
        )
    else:
        # 没有测试：至少保证语法正确（守门员式弱验收）
        excluded = r"(\.venv|\.forge|\.pico|\.git|node_modules|__pycache__)"
        verify_command = f"python -m compileall -q -f . -x \"{excluded}\""
        change_policy = ChangePolicy(allowed_paths=["*"], forbidden_paths=[])

    return AcceptanceContract(
        goal=goal,
        default_contract=True,
        reproduce_command=verify_command,
        reproduce_timeout=60,
        reproduce=ReproduceExpectation(outcome="target_failure", exit_code=1),
        verify_commands=[VerifyCommand(command=verify_command, timeout_seconds=120)],
        change_policy=change_policy,
    )


# ---------------------------------------------------------------------------
# ContractStore
# ---------------------------------------------------------------------------

class ContractStore:
    """contract.json 的持久化与校验。"""

    def __init__(self, tasks_root: Path):
        self._tasks_root = Path(tasks_root).resolve()

    def save_contract(
        self, task_id: str, contract: AcceptanceContract, allow_replace: bool = False
    ) -> Path:
        """原子写入 contract.json；除非明确允许，否则拒绝覆盖已有合约。

        使用与 canonical_hash 一致的 compact JSON 格式，
        使 raw hash 与程序内计算的 hash 一致。
        """
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / "contract.json"
        if path.exists() and not allow_replace:
            raise FileExistsError(f"contract already exists for task: {task_id}")

        payload = json.dumps(contract.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        tmp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=str(path.parent),
                prefix=path.name + ".", suffix=".tmp"
            ) as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
                tmp_name = f.name
            Path(tmp_name).replace(path)
            # 同步目录项，避免崩溃时丢失 rename；部分 Windows 文件系统不支持目录 fsync。
            try:
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
            return path
        except Exception:
            if tmp_name:
                Path(tmp_name).unlink(missing_ok=True)
            raise

    def load_contract(self, task_id: str) -> AcceptanceContract | None:
        """加载 contract.json，不存在时返回 None。"""
        path = self._contract_path(task_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return AcceptanceContract.from_dict(data)

    def contract_hash(self, task_id: str) -> str | None:
        """返回当前 contract.json 原始内容的 SHA-256（拒绝未知字段绕过）。"""
        path = self._contract_path(task_id)
        if not path.exists():
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def verify_integrity(self, task_id: str, expected_hash: str) -> bool:
        """校验 contract.json 的原始内容哈希是否匹配。"""
        actual = self.contract_hash(task_id)
        return actual == expected_hash

    def _contract_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "contract.json"

    def _task_dir(self, task_id: str) -> Path:
        return self._tasks_root / _safe_segment(task_id)


def _safe_segment(task_id: str) -> str:
    value = str(task_id or "").strip()
    if not value or value in {".", ".."}:
        raise ValueError(f"invalid task_id: {task_id}")
    return value.replace("/", "_").replace("\\", "_")


def _normalize_relative_path(path: str) -> str:
    """将 Git 输出规范化为相对 POSIX 路径。

    只剥离前导 ``./``（git 相对输出不携带），保留前导点目录名，
    例如 ``.forge/tasks/...`` 不能被归一化成 ``forge/...``。
    """
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _path_glob_matches(path: str, pattern: str) -> bool:
    """使用完整路径 glob 匹配，其中 ** 可跨越任意目录层级。"""
    path_parts = tuple(part for part in path.split("/") if part)
    pattern_parts = tuple(part for part in pattern.split("/") if part)

    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        current = pattern_parts[pattern_index]
        if current == "**":
            return any(matches(index, pattern_index + 1) for index in range(path_index, len(path_parts) + 1))
        return (
            path_index < len(path_parts)
            and PurePosixPath(path_parts[path_index]).match(current)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def _safe_evidence_stem(value: str, default: str = "command") -> str:
    """生成跨平台安全且稳定的证据文件名片段。"""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value).strip()).strip(".-")
    return stem[:80] or default


# ---------------------------------------------------------------------------
# CommandVerifier —— 独立执行命令并裁决
# ---------------------------------------------------------------------------

class CommandVerifier:
    """在独立子进程中执行命令，返回 VerifierVerdict + 证据。

    使用过滤后的环境变量运行，避免泄露主机 secrets。
    """

    _ALLOWED_ENV = frozenset({
        "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE",
        "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME",
        "PWD", "SHELL", "TERM",
    })

    def __init__(self, cwd: str | Path | None = None):
        self.cwd = Path(cwd).resolve() if cwd else None

    def run(self, command: str, timeout_seconds: int = 60) -> tuple[VerifierVerdict, CommandResult]:
        """执行命令并产生裁决。"""
        started = datetime.now(timezone.utc).isoformat()
        try:
            proc = subprocess.run(
                command,
                cwd=self.cwd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=self._sanitized_env(),
            )
            finished = datetime.now(timezone.utc).isoformat()
            result = CommandResult(
                command=command,
                exit_code=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                timeout_seconds=timeout_seconds,
                started_at=started,
                finished_at=finished,
                cwd=str(self.cwd or Path.cwd()),
                platform=platform.platform(),
                python_version=platform.python_version(),
            )
            if proc.returncode == 0:
                return VerifierVerdict.PASS, result
            if self._looks_like_command_not_found(result.stderr):
                return VerifierVerdict.INFRA_ERROR, result
            return VerifierVerdict.FAIL, result
        except subprocess.TimeoutExpired:
            finished = datetime.now(timezone.utc).isoformat()
            result = CommandResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr="[timed out]",
                timeout_seconds=timeout_seconds,
                timed_out=True,
                started_at=started,
                finished_at=finished,
                cwd=str(self.cwd or Path.cwd()),
                platform=platform.platform(),
                python_version=platform.python_version(),
            )
            return VerifierVerdict.INFRA_ERROR, result
        except FileNotFoundError:
            finished = datetime.now(timezone.utc).isoformat()
            result = CommandResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=f"command not found: {command}",
                timeout_seconds=timeout_seconds,
                started_at=started,
                finished_at=finished,
                cwd=str(self.cwd or Path.cwd()),
                platform=platform.platform(),
                python_version=platform.python_version(),
            )
            return VerifierVerdict.INFRA_ERROR, result
        except OSError as exc:
            finished = datetime.now(timezone.utc).isoformat()
            result = CommandResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=f"command infrastructure error: {exc}",
                timeout_seconds=timeout_seconds,
                started_at=started,
                finished_at=finished,
                cwd=str(self.cwd or Path.cwd()),
                platform=platform.platform(),
                python_version=platform.python_version(),
            )
            return VerifierVerdict.INFRA_ERROR, result

    @staticmethod
    def _looks_like_command_not_found(stderr: str) -> bool:
        """将 shell 层面的命令缺失与业务命令失败区分开。"""
        text = str(stderr).lower()
        markers = (
            "command not found",
            "is not recognized as an internal or external command",
            "not recognized as the name of a cmdlet",
        )
        return any(marker in text for marker in markers)

    def _sanitized_env(self) -> dict[str, str]:
        """返回过滤后的环境变量，只保留白名单项。"""
        clean = {}
        for key in self._ALLOWED_ENV:
            val = os.environ.get(key)
            if val is not None:
                clean[key] = val
        # 始终设置 PATH 以防命令找不到
        if "PATH" not in clean:
            clean["PATH"] = os.environ.get("PATH", "")
        return clean


# ---------------------------------------------------------------------------
# Reproducer —— 判断目标故障能否稳定复现
# ---------------------------------------------------------------------------

class Reproducer:
    """在 Agent 修改前执行 reproduce 命令，判断目标故障能否稳定复现。"""

    def __init__(self, cwd: str | Path | None = None, stability_runs: int = 2):
        self._verifier = CommandVerifier(cwd=cwd)
        self.stability_runs = max(1, int(stability_runs))
        self.last_results: list[CommandResult] = []

    def reproduce(self, contract: AcceptanceContract) -> tuple[ReproductionVerdict, CommandResult | None]:
        """执行重复 reproduce，识别稳定目标失败、未复现与 flaky。"""
        self.last_results = []
        if not contract.reproduce_command:
            return ReproductionVerdict.AMBIGUOUS, None

        observations: list[ReproductionVerdict] = []
        for _ in range(self.stability_runs):
            verdict, result = self._verifier.run(
                contract.reproduce_command, contract.reproduce_timeout
            )
            self.last_results.append(result)
            if verdict == VerifierVerdict.INFRA_ERROR:
                return ReproductionVerdict.INFRA_ERROR, result
            if verdict == VerifierVerdict.PASS:
                observations.append(ReproductionVerdict.NOT_REPRODUCED)
            elif contract.reproduce and contract.reproduce.matches(
                result.exit_code, result.stdout, result.stderr
            ):
                observations.append(ReproductionVerdict.REPRODUCED)
            else:
                observations.append(ReproductionVerdict.AMBIGUOUS)

        if len(set(observations)) > 1:
            return ReproductionVerdict.FLAKY, self.last_results[-1]
        return observations[0], self.last_results[-1]


# ---------------------------------------------------------------------------
# ChangeScopeVerifier —— 检查文件修改范围
# ---------------------------------------------------------------------------

class ChangeScopeVerifier:
    """基于真实 workspace diff 检查修改范围，不依赖 Agent 自报。"""

    def __init__(self, workspace_root: Path):
        self.root = workspace_root.resolve()

    def check(
        self, contract: AcceptanceContract, changed_files: list[str] | None = None
    ) -> ChangeScopeVerdict:
        """检查工作区的实际变更是否符合 Contract 的路径约束。"""
        changed = self._get_changed_files() if changed_files is None else sorted(set(changed_files))
        if not changed:
            return ChangeScopeVerdict.ALLOWED

        forbidden = contract.change_policy.forbidden_paths
        allowed = contract.change_policy.allowed_paths

        for f in changed:
            if self._matches_pattern(f, forbidden):
                return ChangeScopeVerdict.FORBIDDEN_MODIFIED
            if "*" not in allowed and not self._matches_pattern(f, allowed):
                return ChangeScopeVerdict.ALLOWED_OUTSIDE_SCOPE

        return ChangeScopeVerdict.ALLOWED

    def diff_summary(self) -> list[str]:
        """返回当前工作区的文件变更列表。"""
        return self._get_changed_files()

    def is_contract_path_modified(self, task_id: str) -> bool:
        """检查受保护的 .forge/tasks/<task_id>/contract.json 是否被工作区修改。

        注意：``_get_changed_files()`` 已排除 ``.forge/`` runtime 目录，
        因此该 git 变更检测通常不会命中；Contract 不可变的权威守卫是
        ``_contract_is_trusted()``（内容哈希比对）。此处保留路径归一化
        一致性，供 .forge 被显式纳入版本控制的仓库使用。
        """
        contract_path = _normalize_relative_path(
            (PurePosixPath(".forge/tasks") / _safe_segment(task_id) / "contract.json").as_posix()
        )
        return contract_path in self._get_changed_files()

    def _get_changed_files(self) -> list[str]:
        """汇总 unstaged、staged 与 untracked 文件，不因某一类非空而短路。

        Runtime 状态目录（``.forge/`` / ``.pico/``）不属于业务变更：
        - 排除它们后，ChangeScopeVerifier 不会把运行时状态误判为 scope 违规；
        - Contract 完整性由 ``_contract_is_trusted()`` 的内容哈希守护，
          git 变更检测只是辅助手段（.forge 在真实仓库中通常被 gitignore）。
        """
        commands = (
            ["git", "diff", "--name-only", "--relative"],
            ["git", "diff", "--cached", "--name-only", "--relative"],
            ["git", "ls-files", "--others", "--exclude-standard"],
        )
        files: set[str] = set()
        try:
            for command in commands:
                result = subprocess.run(
                    command, cwd=self.root, capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    files.update(
                        _normalize_relative_path(item)
                        for item in result.stdout.splitlines() if item.strip()
                    )
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        return sorted(
            f for f in files
            if not f.startswith((".forge/", ".pico/"))
        )

    @staticmethod
    def _matches_pattern(path: str, patterns: list[str]) -> bool:
        """按 POSIX 路径 glob 匹配，避免将模式作为任意子串处理。"""
        normalized_path = _normalize_relative_path(path)
        for pattern in patterns:
            normalized_pattern = _normalize_relative_path(pattern)
            if normalized_pattern and _path_glob_matches(normalized_path, normalized_pattern):
                return True
        return False


# ---------------------------------------------------------------------------
# EvidenceRecorder —— 保存验证证据
# ---------------------------------------------------------------------------

class EvidenceRecorder:
    """将验证结果保存为可追溯的证据 artifact。

    保存前会对常见敏感模式做脱敏处理，防止环境变量或 API key
    意外出现在 evidence 文件中。
    """

    # 已知敏感的 header / key 的正则模式
    _SENSITIVE_PATTERNS = [
        (re.compile(r'(api[_-]?key|token|secret|password)\s*[:=]\s*\S+', re.IGNORECASE),
         lambda m: m.group(1) + "=<redacted>"),
        (re.compile(r'(FORGE_|PICO_|OPENAI_|ANTHROPIC_|DEEPSEEK_)(API_KEY|TOKEN|SECRET)\s*[:=]\s*\S+', re.IGNORECASE),
         lambda m: m.group(1) + m.group(2) + "=<redacted>"),
        (re.compile(r'Authorization\s*:\s*Bearer\s+\S+', re.IGNORECASE),
         lambda m: "Authorization: Bearer <redacted>"),
        (re.compile(r'(?:_|^)(?:API_KEY|TOKEN|SECRET|PASSWORD|PAT|CREDENTIAL)\s*[:=]\s*\S+', re.IGNORECASE),
         lambda m: m.group(0).split("=")[0] + "=<redacted>"),
        # 常见 token 前缀和无标签高熵凭据，覆盖 `echo sk-...` 这类输出。
        (re.compile(r'\b(?:sk|rk|gh[pousr]|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b', re.IGNORECASE),
         lambda _m: "<redacted>"),
        (re.compile(r'\b(?:api[-_]?key|token|secret|password)[-_][A-Za-z0-9_-]{4,}\b', re.IGNORECASE),
         lambda _m: "<redacted>"),
    ]

    def __init__(self, tasks_root: Path, secret_values: list[str] | None = None):
        self._tasks_root = Path(tasks_root).resolve()
        self._secret_values = sorted(
            {str(value) for value in (secret_values or []) if str(value)},
            key=len,
            reverse=True,
        )

    @staticmethod
    def redact(text: str, secret_values: list[str] | None = None) -> str:
        """对文本执行脱敏，替换已知模式和 Runtime 提供的精确值。"""
        text = str(text)
        for value in sorted({str(item) for item in (secret_values or []) if str(item)}, key=len, reverse=True):
            text = text.replace(value, "<redacted>")
        for pattern, replacer in EvidenceRecorder._SENSITIVE_PATTERNS:
            text = pattern.sub(replacer, text)
        # 同时脱敏常见 env 文件格式的敏感赋值语句
        text = re.sub(
            r'(?:^|\n)\s*(export\s+)?'
            r'(FORGE_|PICO_|OPENAI_|ANTHROPIC_|DEEPSEEK_|GITHUB_)?'
            r'(API_KEY|TOKEN|SECRET|PASSWORD|PAT)\s*[=:]\s*[^\s"\']+',
            r'\1\2\3=<redacted>',
            text,
            flags=re.IGNORECASE,
        )
        return text

    def save_evidence(self, task_id: str, stem: str, result: CommandResult) -> Path:
        """保存命令执行结果到 evidence/ 目录。"""
        evidence_dir = self._evidence_dir(task_id)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / f"{stem}.txt"
        text = self.redact(result.to_text(), self._secret_values)
        path.write_text(text, encoding="utf-8")
        return path

    def save_verdict(self, task_id: str, verdict: VerifierVerdict | ReproductionVerdict,
                     detail: str = "", refs: list[str] | None = None) -> Path:
        """保存裁决摘要到 evidence/。"""
        evidence_dir = self._evidence_dir(task_id)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / f"verdict-{_now_safe()}.txt"
        lines = [
            f"verdict: {verdict.value if isinstance(verdict, enum.Enum) else verdict}",
            f"detail: {self.redact(detail, self._secret_values)}",
        ]
        if refs:
            for r in refs:
                lines.append(f"ref: {r}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _evidence_dir(self, task_id: str) -> Path:
        return self._tasks_root / _safe_segment(task_id) / "evidence"


def _now_safe() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


# ---------------------------------------------------------------------------
# TaskVerificationService —— reproduce + verify 编排
# ---------------------------------------------------------------------------

@dataclass
class BaselineInfo:
    """基线信息，记录在 TaskRecord 中。"""
    commit: str = ""
    workspace_fingerprint: str = ""
    reproduction_verdict: str = ""
    failure_fingerprint: str = ""
    evidence_ref: str = ""
    workspace_files: dict[str, str] = field(default_factory=dict)


@dataclass
class LastVerdictInfo:
    """最近一次验证结果。"""
    verdict: str = ""
    checked_at: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    reason: str = ""


class TaskVerificationService:
    """Task 级别的验证编排。

    职责：
    1. 固定基线（reproduce）
    2. 执行验收（verify）
    3. 保存证据
    4. 更新 Task 的 baseline / last_verdict

    PR 2 不会自动启动下一轮 Run。
    """

    def __init__(self, task_store, contract_store: ContractStore,
                 workspace_root: Path, tasks_root: Path,
                 expected_contract_hash: str | None = None,
                 secret_values: list[str] | None = None):
        self.task_store = task_store
        self.contract_store = contract_store
        self.workspace_root = Path(workspace_root).resolve()
        self.tasks_root = Path(tasks_root).resolve()
        self._expected_contract_hash = expected_contract_hash
        self._verifier = CommandVerifier(cwd=self.workspace_root)
        self._reproducer = Reproducer(cwd=self.workspace_root)
        self._scope = ChangeScopeVerifier(self.workspace_root)
        self._evidence = EvidenceRecorder(
            self.tasks_root, secret_values=secret_values or secret_env_values()
        )

    def set_expected_contract_hash(self, expected_contract_hash: str | None) -> None:
        """设置后续验证必须匹配的受信任合约哈希。"""
        self._expected_contract_hash = expected_contract_hash

    def contract_is_intact(self, task_id: str, expected_contract_hash: str) -> bool:
        """供 CLI 在启动 Agent 前检查 Runtime 冻结的 Contract。"""
        return bool(expected_contract_hash) and self.contract_store.verify_integrity(
            task_id, expected_contract_hash
        )

    def establish_baseline(
        self, task_id: str, expected_contract_hash: str | None = None
    ) -> tuple[ReproductionVerdict, BaselineInfo]:
        """执行 reproduce，固定基线信息。

        应在 Agent 修改前调用。返回裁决和基线信息。
        """
        contract = self.contract_store.load_contract(task_id)
        if contract is None:
            return ReproductionVerdict.AMBIGUOUS, BaselineInfo()
        if expected_contract_hash is not None:
            self.set_expected_contract_hash(expected_contract_hash)
        # 建立基线是受信任方固定合约哈希的时机。
        if self._expected_contract_hash is None:
            self._expected_contract_hash = contract.canonical_hash
        if not self._contract_is_trusted(task_id) or self._scope.is_contract_path_modified(task_id):
            return ReproductionVerdict.AMBIGUOUS, BaselineInfo()

        commit = self._get_commit()
        workspace_files = self._workspace_file_manifest()
        fingerprint = self._manifest_fingerprint(workspace_files)

        repro_verdict, result = self._reproducer.reproduce(contract)

        info = BaselineInfo(
            commit=commit,
            workspace_fingerprint=fingerprint,
            workspace_files=workspace_files,
            reproduction_verdict=repro_verdict.value,
            failure_fingerprint="",
            evidence_ref="",
        )

        results = list(self._reproducer.last_results)
        if results:
            refs = []
            for index, command_result in enumerate(results, start=1):
                path = self._evidence.save_evidence(
                    task_id, f"baseline-reproduce-{index}", command_result
                )
                refs.append(str(path.relative_to(self.tasks_root)))
            info.evidence_ref = refs[0]

            # 提取失败指纹（用最终一次稳定目标失败的 stdout/stderr）。
            if repro_verdict == ReproductionVerdict.REPRODUCED:
                sig = results[-1].stdout + results[-1].stderr
                info.failure_fingerprint = hashlib.sha256(sig.encode()).hexdigest()[:12]

        return repro_verdict, info

    def run_verification(
        self,
        task_id: str,
        run_id: str = "",
        expected_contract_hash: str | None = None,
        baseline_files: dict[str, str] | None = None,
    ) -> tuple[VerifierVerdict, LastVerdictInfo]:
        """执行 Contract 中定义的全部验收命令。

        返回最终裁决（所有命令通过 → PASS，任意失败 → FAIL）。
        """
        if expected_contract_hash is not None:
            self.set_expected_contract_hash(expected_contract_hash)
        contract = self.contract_store.load_contract(task_id)
        if contract is None:
            return self._blocked(task_id, "contract_not_found")
        if not self._contract_is_trusted(task_id):
            return self._blocked(task_id, "contract_hash_mismatch")
        if self._scope.is_contract_path_modified(task_id):
            return self._blocked(task_id, "contract_path_modified")
        if not contract.verify_commands:
            return self._blocked(task_id, "no_verify_commands")

        # 1. 检查修改范围。若已有基线快照，优先比较真实文件内容，
        # 这样 Git 之外的工作区以及 baseline 前已有的脏改动都可正确处理。
        changed_files = (
            self._changed_since_manifest(baseline_files)
            if baseline_files is not None
            else None
        )
        scope = self._scope.check(contract, changed_files=changed_files)
        if scope != ChangeScopeVerdict.ALLOWED:
            path = self._evidence.save_verdict(task_id, VerifierVerdict.FAIL, scope.value)
            return VerifierVerdict.FAIL, LastVerdictInfo(
                verdict=scope.value,
                checked_at=datetime.now(timezone.utc).isoformat(),
                evidence_refs=[str(path.relative_to(self.tasks_root))],
                reason=scope.value,
            )

        # 2. 执行所有验收命令，并保留基础设施错误。
        refs = []
        command_verdicts = []
        for index, vc in enumerate(contract.verify_commands, start=1):
            v, r = self._verifier.run(vc.command, vc.timeout_seconds)
            stem = _safe_evidence_stem(
                f"verify-{run_id or 'manual'}-{index}-{vc.command}"
            )
            path = self._evidence.save_evidence(task_id, stem, r)
            refs.append(str(path.relative_to(self.tasks_root)))
            command_verdicts.append(v)

        if VerifierVerdict.INFRA_ERROR in command_verdicts:
            verdict = VerifierVerdict.INFRA_ERROR
        elif all(v == VerifierVerdict.PASS for v in command_verdicts):
            verdict = VerifierVerdict.PASS
        else:
            verdict = VerifierVerdict.FAIL

        # 3. 所有命令执行结束后再次检查修改范围。
        #    防止 verify command 在执行期间篡改受保护路径（如修改验收脚本本身）。
        post_scope = self._scope.check(contract, changed_files=(
            self._changed_since_manifest(baseline_files) if baseline_files is not None else None
        ))
        if post_scope != ChangeScopeVerdict.ALLOWED:
            # 即使所有命令返回 0，禁区修改也覆盖为 FAIL
            verdict = VerifierVerdict.FAIL
            post_path = self._evidence.save_verdict(
                task_id, VerifierVerdict.FAIL,
                detail=f"post_command_scope_violation: {post_scope.value}"
            )
            refs.append(str(post_path.relative_to(self.tasks_root)))

        info = LastVerdictInfo(
            verdict=verdict.value,
            checked_at=datetime.now(timezone.utc).isoformat(),
            evidence_refs=refs,
        )
        return verdict, info

    # -- helper: 可以验证但不自动写入 Task，留给 CLI/Service 组合使用 --

    def can_verify_only(self, command: str, timeout: int = 60) -> tuple[VerifierVerdict, CommandResult]:
        """快速运行一条验证命令并返回裁决。"""
        return self._verifier.run(command, timeout)

    # -- 内部辅助 ---------------------------------------------------------------

    def _contract_is_trusted(self, task_id: str) -> bool:
        """校验磁盘合约与初始化或调用方提供的可信哈希一致。"""
        expected = self._expected_contract_hash
        return expected is not None and self.contract_store.verify_integrity(task_id, expected)

    def _blocked(self, task_id: str, detail: str) -> tuple[VerifierVerdict, LastVerdictInfo]:
        """保存可审计的阻塞证据并返回 BLOCKED。"""
        path = self._evidence.save_verdict(task_id, VerifierVerdict.BLOCKED, detail)
        return VerifierVerdict.BLOCKED, LastVerdictInfo(
            verdict=VerifierVerdict.BLOCKED.value,
            checked_at=datetime.now(timezone.utc).isoformat(),
            evidence_refs=[str(path.relative_to(self.tasks_root))],
            reason=detail,
        )

    def _workspace_file_manifest(self) -> dict[str, str]:
        """返回除 Runtime 状态外的工作区文件内容哈希，用作可信基线。"""
        ignored_parts = {".git", ".forge", ".pico", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}
        manifest: dict[str, str] = {}
        for path in self.workspace_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(self.workspace_root)
            except ValueError:
                continue
            if any(part in ignored_parts for part in relative.parts):
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            manifest[relative.as_posix()] = digest
        return manifest

    @staticmethod
    def _manifest_fingerprint(manifest: dict[str, str]) -> str:
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:12]

    def _changed_since_manifest(self, baseline_files: dict[str, str]) -> list[str]:
        """基于 baseline 内容快照检测新增、删除和修改的真实文件。"""
        current = self._workspace_file_manifest()
        return sorted(
            path
            for path in set(baseline_files) | set(current)
            if baseline_files.get(path) != current.get(path)
        )

    def _get_commit(self) -> str:
        try:
            r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                               cwd=self.workspace_root, capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (subprocess.SubprocessError, FileNotFoundError):
            return ""

    def _get_workspace_fingerprint(self) -> str:
        try:
            r = subprocess.run(["git", "diff", "--stat"],
                               cwd=self.workspace_root, capture_output=True, text=True, timeout=5)
            return hashlib.sha256(r.stdout.encode()).hexdigest()[:12]
        except (subprocess.SubprocessError, FileNotFoundError):
            return ""
