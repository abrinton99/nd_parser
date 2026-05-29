"""Command-line interface (spec §5)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from . import __version__
from . import exit_codes as ec
from .config import VALID_FIRST_RUN_MODES, load_config
from .inputfile import InputFileError, classify_lines, load_urls
from .logging_setup import get_logger, setup_logging
from .lockfile import LockHeld, acquire_lock, read_lock, release_lock
from .notify import Notifier, RunContext
from .state import Paths, prune_old_runs, update_watcher_state, write_run_summary
from .util import iso, read_json, utc_now


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _load_cfg(ctx_obj, **overrides):
    try:
        return load_config(ctx_obj.get("config"), **overrides)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc))


def _watcher_binary() -> str:
    """Best runnable command for an unattended run, as an absolute path (the
    scheduler starts in $HOME with a minimal PATH, so we never rely on PATH or
    the working dir).

    Preference order:
      1. The console script next to the running interpreter (e.g. the venv's
         `bin/nextdoor-watcher`) — works from any directory.
      2. A `nextdoor-watcher` found on PATH.
      3. `cd <package parent> && <python> -m nextdoor_watcher` — the module form
         needs the package importable, so we cd into its parent first. We do NOT
         resolve() sys.executable: that would follow a venv symlink back to the
         system python, which lacks the package.
    """
    import shutil

    sibling = Path(sys.executable).parent / "nextdoor-watcher"
    if sibling.exists():
        return str(sibling)

    found = shutil.which("nextdoor-watcher")
    if found:
        return str(Path(found).resolve())

    import nextdoor_watcher
    pkg_parent = Path(nextdoor_watcher.__file__).resolve().parent.parent
    return f"cd {pkg_parent} && {sys.executable} -m nextdoor_watcher"


def _log_tail(log_file: Path, n: int = 20) -> str:
    try:
        lines = log_file.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def _notify(cfg, paths: Paths, *, run_id, exit_code, started_iso, duration,
            summary_text) -> None:
    if not cfg.notify.enabled:
        return
    notifier = Notifier(cfg.notify, paths.notifications_state, get_logger())
    ctx = RunContext(
        run_id=run_id,
        exit_code=exit_code,
        started_at=started_iso,
        ended_at=iso(),
        duration_seconds=int(round(duration)),
        input_file=str(cfg.input_file or ""),
        output_dir=str(cfg.output_dir),
        summary_text=summary_text,
        log_tail=_log_tail(paths.log_file),
    )
    notifier.notify_run(ctx)


# --------------------------------------------------------------------------- #
# Root group
# --------------------------------------------------------------------------- #
@click.group()
@click.version_option(__version__, prog_name="nextdoor-watcher")
@click.option("--config", "config_path", type=click.Path(), default=None,
              help="Path to nextdoor-watcher.toml (default: ./nextdoor-watcher.toml).")
@click.pass_context
def cli(ctx, config_path):
    """One-shot, cron-friendly Nextdoor comment screenshotter."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config_path


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
@cli.command()
@click.pass_context
def login(ctx):
    """Open a browser, let the user sign in, and save the session."""
    cfg = _load_cfg(ctx.obj)
    log = setup_logging(None, quiet=False)
    try:
        from .browser import do_login
    except ImportError as exc:
        raise click.ClickException(f"Playwright not installed: {exc}")
    do_login(cfg.storage_state_path, log=log)


# --------------------------------------------------------------------------- #
# scrape
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--input-file", type=click.Path(), default=None)
@click.option("--output-dir", type=click.Path(), default=None)
@click.option("--first-run-mode", type=click.Choice(VALID_FIRST_RUN_MODES), default=None)
@click.option("--dry-run", is_flag=True, default=False,
              help="Do everything except writing PNG/JSON/state files.")
@click.option("--quiet", is_flag=True, default=False, help="Suppress stdout; log to file only.")
@click.option("--headed", is_flag=True, default=False,
              help="Run with a visible browser window (default is headless). For debugging.")
@click.pass_context
def scrape(ctx, input_file, output_dir, first_run_mode, dry_run, quiet, headed):
    """Poll every watched URL once and capture new/edited/deleted comments."""
    cfg = _load_cfg(
        ctx.obj, input_file=input_file, output_dir=output_dir,
        first_run_mode=first_run_mode, quiet=(quiet or None),
    )
    paths = Paths(cfg.output_dir)
    log = setup_logging(paths.log_file, quiet=cfg.quiet)

    if not cfg.input_file:
        log.error("ERROR no input file configured (use --input-file or config).")
        sys.exit(ec.BAD_INPUT)

    # Prune old run summaries at the start of normal runs (spec §20.6).
    try:
        prune_old_runs(paths)
    except OSError:
        pass

    # 1. Acquire the lock.
    try:
        lock = acquire_lock(paths.lock, warn=log.warning)
    except LockHeld as held:
        log.warning(f"another scrape is in progress (pid={held.pid}); exiting")
        sys.exit(ec.SKIPPED)

    started = utc_now()
    started_iso = iso(started)
    run_id = started_iso
    exit_code = ec.OK
    results = []
    summary_text = ""

    from .scrape import ScrapeOptions, run_urls

    try:
        # 2. Session.
        if not cfg.storage_state_path.exists():
            log.error("ERROR session storage_state.json missing — run `nextdoor-watcher login`.")
            exit_code = ec.AUTH
            summary_text = f"storage_state missing at {cfg.storage_state_path}"
            raise _EarlyExit()

        # 3. Input.
        try:
            urls = load_urls(cfg.input_file, warn=log.warning)
        except InputFileError as exc:
            log.error(f"ERROR {exc}")
            exit_code = ec.BAD_INPUT
            summary_text = str(exc)
            raise _EarlyExit()

        log.info(f"run start run_id={run_id} urls={len(urls)} input={Path(cfg.input_file).resolve()}")

        # 4. Scrape.
        opts = ScrapeOptions(dry_run=dry_run, headless=not headed)
        try:
            results, fatal = run_urls(urls, cfg, paths, run_id, opts)
        except ImportError as exc:
            log.error(f"ERROR Playwright not available: {exc}")
            exit_code = ec.RUNTIME
            summary_text = f"Playwright not available: {exc}"
            raise _EarlyExit()

        # 5. Exit code.
        if fatal or any(r.fatal == "session_expired" for r in results):
            exit_code = ec.AUTH
            summary_text = "Session expired. Run `nextdoor-watcher login` to refresh."
        elif all(r.ok for r in results):
            exit_code = ec.OK
        else:
            exit_code = ec.PARTIAL
            failed = [r.url for r in results if not r.ok]
            summary_text = f"{len(failed)} URL(s) failed after retries: {', '.join(failed)}"

    except _EarlyExit:
        pass
    except KeyboardInterrupt:
        log.warning("interrupted (SIGINT); persisting state and releasing lock")
        exit_code = ec.SIGINT
        summary_text = "Interrupted by SIGINT."
    except Exception as exc:  # noqa: BLE001 — top-level safety net
        log.error(f"ERROR unrecoverable runtime error: {exc}")
        exit_code = ec.RUNTIME
        summary_text = f"Unrecoverable runtime error: {exc}"
    finally:
        duration = (utc_now() - started).total_seconds()
        from .scrape import _summarize
        summary = _summarize(run_id, started_iso, results, duration, exit_code)
        if not dry_run:
            try:
                write_run_summary(paths, summary)
                update_watcher_state(paths, input_file=str(cfg.input_file or ""), summary=summary)
            except OSError as exc:
                log.error(f"could not write run state: {exc}")

        t = summary["totals"]
        log.info(
            f"run end run_id={run_id} new={t['new']} edited={t['edited']} "
            f"deleted={t['deleted']} took={int(round(duration))}s exit={exit_code}"
        )

        try:
            _notify(cfg, paths, run_id=run_id, exit_code=exit_code,
                    started_iso=started_iso, duration=duration, summary_text=summary_text)
        except Exception as exc:  # notifications must never crash the run
            log.error(f"notification error: {exc}")

        release_lock(lock)

    sys.exit(exit_code)


class _EarlyExit(Exception):
    """Internal control-flow to jump to the finally block with a set exit code."""


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--input-file", type=click.Path(), required=True)
@click.pass_context
def validate(ctx, input_file):
    """Check every line of the input file and report OK / SKIP / BAD."""
    p = Path(input_file)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        click.echo(f"cannot read {input_file}: {exc}", err=True)
        sys.exit(ec.BAD_INPUT)

    all_ok = True
    for r in classify_lines(text):
        if r.status == "SKIP":
            continue
        if r.status == "OK":
            click.echo(f"OK   line {r.lineno}: {r.url}")
        elif r.status == "DUP":
            click.echo(f"SKIP line {r.lineno}: duplicate ({r.reason})")
        else:  # BAD
            all_ok = False
            click.echo(f"BAD  line {r.lineno}: {r.raw.strip()} — {r.reason}")
    sys.exit(ec.OK if all_ok else ec.BAD_INPUT)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--output-dir", type=click.Path(), default=None)
@click.pass_context
def status(ctx, output_dir):
    """Print the last run, watched posts, session age, and lock state."""
    cfg = _load_cfg(ctx.obj, output_dir=output_dir)
    paths = Paths(cfg.output_dir)

    state = read_json(paths.watcher_state, {}) or {}
    click.echo("== Last run ==")
    if state:
        click.echo(f"  run_id:    {state.get('last_run_id')}")
        click.echo(f"  duration:  {state.get('last_run_duration_seconds')}s")
        click.echo(f"  exit_code: {state.get('last_run_exit_code')}")
        click.echo(f"  new={state.get('last_run_new_count')} "
                   f"edited={state.get('last_run_edited_count')} "
                   f"deleted={state.get('last_run_deleted_count')}")
        click.echo(f"  input:     {state.get('last_input_file')}")
        click.echo(f"  total_runs:{state.get('total_runs')}")
    else:
        click.echo("  (no runs recorded yet)")

    click.echo("\n== Watched posts ==")
    posts_dir = paths.root / "posts"
    if posts_dir.exists():
        for meta_file in sorted(posts_dir.glob("*/post-meta.json")):
            m = read_json(meta_file, {}) or {}
            click.echo(f"  {m.get('post_url')}")
            click.echo(f"    last_scrape={m.get('last_scrape_at')} "
                       f"total_seen={m.get('total_seen')} "
                       f"live={m.get('total_currently_live')} "
                       f"deleted={m.get('total_deleted_ever')}")
    else:
        click.echo("  (none)")

    click.echo("\n== Session ==")
    ss = cfg.storage_state_path
    if ss.exists():
        age_days = (utc_now().timestamp() - ss.stat().st_mtime) / 86400.0
        flag = " (may be expired)" if age_days > 14 else ""
        click.echo(f"  storage_state: {ss} — {age_days:.1f} days old{flag}")
    else:
        click.echo(f"  storage_state: MISSING ({ss}) — run `login`")

    click.echo("\n== Lock ==")
    lock = read_lock(paths.lock)
    if lock:
        click.echo(f"  held by pid={lock.get('pid')} heartbeat={lock.get('heartbeat')}")
    else:
        click.echo("  not held")

    nstate = read_json(paths.notifications_state, {}) or {}
    if nstate.get("smtp_send_failures"):
        click.echo(f"\n== Notifications ==\n  smtp_send_failures={nstate['smtp_send_failures']}")


# --------------------------------------------------------------------------- #
# install-schedule
# --------------------------------------------------------------------------- #
def _wsl_distro() -> str | None:
    """The current WSL distro name, if we're running inside WSL."""
    return os.environ.get("WSL_DISTRO_NAME")


def _build_wrapper(binary: str, config_opt: str, in_abs: Path, out_abs: Path,
                   base_dir: Path, python_exe: str) -> str:
    """A bash wrapper that runs one scrape inside WSL. Windows Task Scheduler
    invokes it via wsl.exe. It cd's into the project dir (so the relative
    ./session login path resolves no matter the launch cwd), sources the SMTP
    env file, runs the watcher, regenerates the data browser, then exits with
    the scrape's exit code so it becomes the task's 'Last Run Result'."""
    return (
        "#!/usr/bin/env bash\n"
        "# Generated by `nextdoor-watcher install-schedule`.\n"
        "# Runs ONE scrape inside WSL; Windows Task Scheduler launches it via wsl.exe.\n"
        "# Re-generate this file if you move the venv, watchlist, or output dir.\n"
        "\n"
        "# Task Scheduler launches WSL from an arbitrary cwd; cd into the project\n"
        "# so the session directory (and any other relative paths) resolve.\n"
        f"cd {base_dir} || exit 5\n"
        "\n"
        '# Load the SMTP password (and any other secrets) if the env file exists.\n'
        "# `set -a` auto-exports everything the file sets, so plain `KEY=value`\n"
        "# lines reach the exec'd watcher process (a bare source would leave them\n"
        "# as non-exported shell vars, and the scrape would hit SMTP 530).\n"
        'if [ -f "$HOME/.nextdoor-watcher.env" ]; then\n'
        "    set -a\n"
        '    . "$HOME/.nextdoor-watcher.env"\n'
        "    set +a\n"
        "fi\n"
        "\n"
        "# Small random delay so the traffic isn't perfectly metronomic.\n"
        "sleep $((RANDOM % 60))\n"
        "\n"
        f"{binary} {config_opt}scrape \\\n"
        f"    --input-file {in_abs} \\\n"
        f"    --output-dir {out_abs} \\\n"
        f"    --quiet >> {out_abs / 'logs' / 'cron.out'} 2>&1\n"
        "rc=$?\n"
        "\n"
        "# Regenerate the static data browser (index.html) from the fresh data.\n"
        "# Best-effort: a build failure must not change the task's result, so we\n"
        "# swallow its exit code and report the scrape's via `exit $rc`.\n"
        f"{python_exe} {base_dir / 'build_browser.py'} \\\n"
        f"    --data {out_abs} --out {base_dir / 'index.html'} \\\n"
        f"    >> {out_abs / 'logs' / 'cron.out'} 2>&1 || true\n"
        "\n"
        "exit $rc\n"
    )


@cli.command(name="install-schedule")
@click.option("--input-file", type=click.Path(), required=True)
@click.option("--every", type=int, required=True, help="Interval in minutes (min 2).")
@click.option("--output-dir", type=click.Path(), default=None)
@click.option("--task-name", default="NextdoorWatcher", show_default=True,
              help="Name for the Windows scheduled task.")
@click.pass_context
def install_schedule(ctx, input_file, every, output_dir, task_name):
    """Write a WSL wrapper script and print the Windows Task Scheduler commands.

    WSL has no reliable always-on cron, so scheduling is owned by Windows Task
    Scheduler, which launches the wrapper via wsl.exe. This writes the wrapper
    and prints the commands to register the task; it does not modify Windows.
    """
    if every < 2:
        click.echo("ERROR: --every must be at least 2 minutes (spec §11).", err=True)
        sys.exit(ec.BAD_INPUT)

    cfg = _load_cfg(ctx.obj, output_dir=output_dir)
    binary = _watcher_binary()
    in_abs = Path(input_file).resolve()
    out_abs = Path(cfg.output_dir).resolve()

    # The scheduler starts in $HOME, so point at the config (which holds the
    # [notify] block) with an absolute path. `--config` is a group-level option,
    # so it goes before the `scrape` subcommand.
    cfg_path = ctx.obj.get("config") or "nextdoor-watcher.toml"
    cfg_abs = Path(cfg_path).resolve() if Path(cfg_path).exists() else None
    config_opt = f"--config {cfg_abs} " if cfg_abs else ""

    # The project dir the session is anchored to: the config file's dir if there
    # is one, else the output dir's parent.
    base_dir = cfg_abs.parent if cfg_abs else out_abs.parent

    # 1. Write the WSL wrapper script.
    wrapper_path = out_abs / "run-watcher.sh"
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(
        _build_wrapper(binary, config_opt, in_abs, out_abs, base_dir, sys.executable),
        encoding="utf-8",
    )
    try:
        os.chmod(wrapper_path, 0o755)
    except OSError:
        pass
    click.echo(f"Wrote WSL wrapper script: {wrapper_path}")

    # 2. Print the Windows Task Scheduler commands.
    distro = _wsl_distro() or "<your-distro>"
    distro_hint = "" if _wsl_distro() else "   # run `wsl -l -q` in Windows to find this"
    wsl_cmd = f"wsl.exe -d {distro} -- bash {wrapper_path}"

    click.echo("\n# --- Register with Windows Task Scheduler ------------------------------")
    click.echo(f"# Run ONE of the following in a Windows terminal (not WSL).{distro_hint}")
    click.echo("\n# Option A — schtasks (cmd.exe or PowerShell):")
    click.echo(
        f'schtasks /Create /TN "{task_name}" /SC MINUTE /MO {every} /F '
        f'/TR "{wsl_cmd}"'
    )
    click.echo("\n# Option B — PowerShell (sub-daily repetition, survives reboots):")
    click.echo(
        f"$a = New-ScheduledTaskAction -Execute 'wsl.exe' "
        f"-Argument '-d {distro} -- bash {wrapper_path}'\n"
        f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {every})\n"
        f"Register-ScheduledTask -TaskName '{task_name}' -Action $a -Trigger $t "
        f"-Description 'Nextdoor comment watcher'"
    )

    if cfg.notify.enabled:
        env_name = cfg.notify.smtp.password_env or "NEXTDOOR_WATCHER_SMTP_PASSWORD"
        click.echo(
            f"\n# SMTP notifications are enabled. The wrapper sources $HOME/.nextdoor-watcher.env;"
            f"\n# put `{env_name}=...` there (chmod 600) so the scheduled run can authenticate."
        )
    click.echo("\n# To remove the task later:  schtasks /Delete /TN \"%s\" /F" % task_name)


# --------------------------------------------------------------------------- #
# test-notify
# --------------------------------------------------------------------------- #
@cli.command(name="test-notify")
@click.option("--output-dir", type=click.Path(), default=None)
@click.pass_context
def test_notify(ctx, output_dir):
    """Send a one-off test email to confirm SMTP is configured (bypasses cooldown)."""
    cfg = _load_cfg(ctx.obj, output_dir=output_dir)
    paths = Paths(cfg.output_dir)
    setup_logging(paths.log_file, quiet=False)

    if not cfg.notify.enabled:
        click.echo("notify.enabled = false; nothing to test.", err=True)
        sys.exit(ec.BAD_INPUT)

    notifier = Notifier(cfg.notify, paths.notifications_state, get_logger())
    ok, detail = notifier.send_test()
    if ok:
        click.echo(f"sent test email to {detail}")
        sys.exit(ec.OK)
    click.echo(f"SMTP error: {detail}", err=True)
    sys.exit(ec.RUNTIME)


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
