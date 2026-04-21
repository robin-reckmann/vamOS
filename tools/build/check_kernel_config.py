#!/usr/bin/env python3
import argparse
import atexit
import errno
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FragmentEntry:
    line_no: int
    symbol: str
    requested: str


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def colorize(color: str, message: str) -> str:
    return f"{color}{message}{RESET}"


def remove_tree(path: Path) -> None:
    def onerror(func, target, exc_info):
        exc = exc_info[1]
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except OSError:
            pass

        if isinstance(exc, OSError) and exc.errno == errno.ENOTEMPTY:
            raise exc

        func(target)

    if not path.exists():
        return

    last_error = None
    for attempt in range(5):
        try:
            shutil.rmtree(path, onerror=onerror)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if exc.errno != errno.ENOTEMPTY:
                raise
            time.sleep(0.2 * (attempt + 1))

    if last_error is not None:
        raise last_error


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    root = repo_root()
    parser = argparse.ArgumentParser(
        description="Check a kernel config fragment for unresolved and redundant settings."
    )
    parser.add_argument(
        "--kernel-dir",
        default=str(root / "kernel/linux"),
        help="Path to the kernel source tree",
    )
    parser.add_argument(
        "--fragment",
        default=str(root / "kernel/configs/vamos.config"),
        help="Path to the config fragment to validate",
    )
    parser.add_argument(
        "--base-defconfig",
        default="defconfig",
        help="Base defconfig target to start from",
    )
    parser.add_argument(
        "--work-dir",
        help="Directory used for temporary config resolution work",
    )
    parser.add_argument(
        "--patch-dir",
        default=str(root / "kernel/patches"),
        help="Directory containing kernel patches applied during builds",
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="Keep the work directory instead of deleting it at the end",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only check the first N fragment settings",
    )
    parser.add_argument(
        "--inside-container",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=capture,
        check=True,
    )


def make_is_usable() -> bool:
    try:
        result = run(["make", "--version"], capture=True)
    except Exception:
        return False

    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    match = re.search(r"GNU Make\s+(\d+)\.(\d+)", first_line)
    return bool(match and int(match.group(1)) >= 4)


def ensure_builder_image(root: Path) -> None:
    try:
        run(["docker", "image", "inspect", "vamos-builder"], cwd=root, capture=True)
        return
    except subprocess.CalledProcessError:
        pass

    log("Building vamos-builder docker image for config checking")
    run(
        [
            "docker",
            "build",
            "-f",
            "tools/build/Dockerfile.builder",
            "-t",
            "vamos-builder",
            str(root),
            "--build-arg",
            f"UNAME={os.environ.get('USER', 'vamos')}",
            "--build-arg",
            f"UID={os.getuid()}",
            "--build-arg",
            f"GID={os.getgid()}",
        ],
        cwd=root,
    )


def docker_volume_exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "volume", "inspect", name],
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0


def prepare_kernel_volume(root: Path, volume_name: str) -> None:
    run(["docker", "volume", "create", volume_name], cwd=root, capture=True)
    run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{volume_name}:/linux",
            "vamos-builder",
            "-lc",
            f"mkdir -p /linux && chown {os.getuid()}:{os.getgid()} /linux && chmod 0775 /linux",
        ],
        cwd=root,
        capture=True,
    )


def seed_kernel_volume(root: Path, kernel_dir: Path, volume_name: str) -> None:
    kernel_rev = run(["git", "-C", str(kernel_dir), "rev-parse", "HEAD"], capture=True).stdout.strip()
    log(f"Seeding kernel volume {volume_name} at {kernel_rev[:12]}")
    sync_container_id = run(
        [
            "docker",
            "run",
            "-d",
            "--entrypoint",
            "tail",
            "-v",
            f"{root}:/repo:ro",
            "-v",
            f"{volume_name}:/linux",
            "vamos-builder",
            "-f",
            "/dev/null",
        ],
        cwd=root,
        capture=True,
    ).stdout.strip()
    try:
        run(["docker", "exec", sync_container_id, "sh", "-lc", "rm -rf /linux/* /linux/.[!.]* /linux/..?*"], cwd=root, capture=True)
        run(
            [
                "docker",
                "exec",
                "-u",
                f"{os.getuid()}:{os.getgid()}",
                sync_container_id,
                "sh",
                "-lc",
                f"cd /linux && git clone --no-local /repo/{kernel_dir.relative_to(root)} . >/dev/null 2>&1 && git checkout --force '{kernel_rev}' >/dev/null 2>&1",
            ],
            cwd=root,
            capture=True,
        )
    finally:
        subprocess.run(["docker", "container", "rm", "-f", sync_container_id], cwd=root, text=True, capture_output=True, check=False)


def kernel_volume_ready(root: Path, kernel_dir: Path, volume_name: str) -> bool:
    if not docker_volume_exists(volume_name):
        return False
    kernel_rev = run(["git", "-C", str(kernel_dir), "rev-parse", "HEAD"], capture=True).stdout.strip()
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{volume_name}:/linux",
            "vamos-builder",
            "-lc",
            f"test \"$(git -c safe.directory=/linux -C /linux rev-parse HEAD 2>/dev/null)\" = \"{kernel_rev}\"",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def cleanup_kernel_volume_tree(root: Path, kernel_dir: Path, volume_name: str) -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-u",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{volume_name}:{kernel_dir}",
            "vamos-builder",
            "-lc",
            f"git -C '{kernel_dir}' reset --hard HEAD >/dev/null 2>&1 || true; "
            f"git -C '{kernel_dir}' clean -fd >/dev/null 2>&1 || true; "
            f"rm -rf '{kernel_dir}/out-config-check'",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def install_exit_cleanup(root: Path, kernel_dir: Path, volume_name: str, container_name: str) -> None:
    cleaned = False

    def cleanup(*_args) -> None:
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        subprocess.run(
            ["docker", "container", "rm", "-f", container_name],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        cleanup_kernel_volume_tree(root, kernel_dir, volume_name)

    def handle_signal(signum, _frame) -> None:
        log(f"Interrupted by signal {signum}, cleaning kernel checker tree")
        cleanup()
        raise SystemExit(128 + signum)

    atexit.register(cleanup)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def reexec_in_container(args: argparse.Namespace) -> None:
    root = repo_root()
    ensure_builder_image(root)
    volume_name = "vamos-kernel-linux-config-check"
    kernel_dir = Path(args.kernel_dir).resolve()
    if not kernel_volume_ready(root, kernel_dir, volume_name):
        prepare_kernel_volume(root, volume_name)
        seed_kernel_volume(root, kernel_dir, volume_name)
    container_name = f"vamos-config-check-{os.getpid()}"
    install_exit_cleanup(root, kernel_dir, volume_name, container_name)

    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--entrypoint",
        "python3",
        "-u",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{root}:{root}",
        "-v",
        f"{volume_name}:{kernel_dir}",
        "-w",
        str(root),
        "vamos-builder",
        str(root / "tools/build/check_kernel_config.py"),
        "--inside-container",
        "--kernel-dir",
        str(Path(args.kernel_dir).resolve()),
        "--fragment",
        str(Path(args.fragment).resolve()),
        "--base-defconfig",
        args.base_defconfig,
        "--patch-dir",
        str(Path(args.patch_dir).resolve()),
    ]
    if args.work_dir is not None:
        cmd.extend(["--work-dir", str(Path(args.work_dir).resolve())])
    if args.keep_work_dir:
        cmd.append("--keep-work-dir")
    if args.limit is not None:
        cmd.extend(["--limit", str(args.limit)])
    raise SystemExit(subprocess.call(cmd))


def parse_fragment(fragment: Path) -> list[FragmentEntry]:
    entries: list[FragmentEntry] = []
    for idx, line in enumerate(fragment.read_text().splitlines(), start=1):
        stripped = line.strip()
        match = re.match(r"^(CONFIG_[A-Z0-9_]+)=(.*)$", stripped)
        if match:
            entries.append(FragmentEntry(idx, match.group(1), match.group(2)))
            continue

        match = re.match(r"^# (CONFIG_[A-Z0-9_]+) is not set$", stripped)
        if match:
            entries.append(FragmentEntry(idx, match.group(1), "n"))
    return entries


def parse_dot_config(config_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in config_path.read_text().splitlines():
        match = re.match(r"^(CONFIG_[A-Z0-9_]+)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2)
            continue
        match = re.match(r"^# (CONFIG_[A-Z0-9_]+) is not set$", line)
        if match:
            values[match.group(1)] = "n"
    return values


def clean_kernel_tree(kernel_dir: Path) -> None:
    run(["git", "-C", str(kernel_dir), "reset", "--hard", "HEAD"], capture=True)
    run(["git", "-C", str(kernel_dir), "clean", "-fd"], capture=True)


def apply_patches(kernel_dir: Path, patch_dir: Path) -> None:
    patch_files = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []
    if patch_files:
        for patch in patch_files:
            run(["git", "apply", "--check", "--whitespace=error", str(patch)], cwd=kernel_dir, capture=True)
            run(["git", "apply", "--whitespace=error", str(patch)], cwd=kernel_dir, capture=True)


def write_fragment_without_line(src: Path, dst: Path, line_no: int) -> None:
    lines = src.read_text().splitlines(keepends=True)
    with dst.open("w") as handle:
        for idx, line in enumerate(lines, start=1):
            if idx != line_no:
                handle.write(line)


def prepare_base_config(kernel_dir: Path, base_defconfig: str, workdir: Path) -> Path:
    if workdir.exists():
        remove_tree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    run(["make", "O=" + str(workdir), base_defconfig], cwd=kernel_dir, capture=True)
    return workdir / ".config"


def resolve_config(kernel_dir: Path, base_config: Path, fragment: Path, workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base_config, workdir / ".config")

    env = os.environ.copy()
    env["KCONFIG_CONFIG"] = str(workdir / ".config")
    run(
        [
            "bash",
            "scripts/kconfig/merge_config.sh",
            "-m",
            "-y",
            str(workdir / ".config"),
            str(fragment),
        ],
        cwd=kernel_dir,
        env=env,
        capture=True,
    )
    run(["make", "O=" + str(workdir), "olddefconfig"], cwd=kernel_dir, capture=True)
    return workdir / ".config"


def state_label(value: str | None) -> str:
    return value if value is not None else "<missing>"


def main() -> int:
    args = parse_args()
    if not args.inside_container and not make_is_usable():
        reexec_in_container(args)

    root = repo_root()
    kernel_dir = Path(args.kernel_dir).resolve()
    fragment = Path(args.fragment).resolve()
    work_root = Path(args.work_dir).resolve() if args.work_dir else kernel_dir / "out-config-check"
    patch_dir = Path(args.patch_dir).resolve()
    entries = parse_fragment(fragment)
    if args.limit is not None:
        entries = entries[: args.limit]

    if not entries:
        print(f"No config settings found in {fragment}")
        return 1

    clean_kernel_tree(kernel_dir)
    try:
        work_root.mkdir(parents=True, exist_ok=True)
        apply_patches(kernel_dir, patch_dir)
        try:
            fragment_display = fragment.relative_to(root)
        except ValueError:
            fragment_display = fragment

        log("Kernel config fragment check")
        log(f"Base defconfig: {args.base_defconfig}")
        log(f"Fragment: {fragment_display}")
        log("")

        base_config_path = prepare_base_config(kernel_dir, args.base_defconfig, work_root / "base")
        full_config_path = resolve_config(kernel_dir, base_config_path, fragment, work_root / "full")
        full_config = parse_dot_config(full_config_path)

        unresolved: list[tuple[FragmentEntry, str | None]] = []
        necessary: list[FragmentEntry] = []
        redundant: list[FragmentEntry] = []

        trial_output_dir = work_root / "trial"
        trial_fragment = work_root / "trial.config"

        for index, entry in enumerate(entries, start=1):
            resolved = full_config.get(entry.symbol)
            if resolved != entry.requested:
                log(
                    colorize(
                        RED,
                        f"[{index}/{len(entries)}] {entry.symbol}: unresolved "
                        f"(requested {entry.requested}, resolved {state_label(resolved)})",
                    )
                )
                unresolved.append((entry, resolved))
                continue

            write_fragment_without_line(fragment, trial_fragment, entry.line_no)
            reduced_config_path = resolve_config(
                kernel_dir,
                base_config_path,
                trial_fragment,
                trial_output_dir,
            )
            reduced_config = parse_dot_config(reduced_config_path)
            if reduced_config.get(entry.symbol) == entry.requested:
                log(colorize(YELLOW, f"[{index}/{len(entries)}] {entry.symbol}: redundant"))
                redundant.append(entry)
            else:
                log(colorize(GREEN, f"[{index}/{len(entries)}] {entry.symbol}: necessary"))
                necessary.append(entry)

        print()
        print("Summary")
        print()

        print(f"Necessary settings ({len(necessary)}):")
        for entry in necessary:
            print(f"  line {entry.line_no}: {entry.symbol}={entry.requested}")
        if not necessary:
            print("  none")
        print()

        print(f"Redundant settings ({len(redundant)}):")
        for entry in redundant:
            print(f"  line {entry.line_no}: {entry.symbol}={entry.requested}")
        if not redundant:
            print("  none")
        print()

        print(f"Unresolved settings ({len(unresolved)}):")
        for entry, resolved in unresolved:
            print(f"  line {entry.line_no}: {entry.symbol} requested {entry.requested}, resolved {state_label(resolved)}")
        if not unresolved:
            print("  none")

        return 1 if unresolved else 0
    finally:
        clean_kernel_tree(kernel_dir)
        if not args.keep_work_dir and work_root.exists():
            remove_tree(work_root)


if __name__ == "__main__":
    sys.exit(main())
