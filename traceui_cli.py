#!/usr/bin/python3

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import adblib

from core.logger_config import LOG_LEVEL_ENV, setup_logger


logger = setup_logger("traceui_cli")

REPO_ROOT = Path(__file__).resolve().parent
PLUGINS_PATH = REPO_ROOT / "plugins"
DEFAULT_SESSION_FILE = REPO_ROOT / "tmp" / "traceui_cli_capture_session.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tmp"
CLI_PLUGIN_ALIASES = {
    "gfxr": "gfxreconstruct",
    "patrace": "patrace",
    "auto": "auto",
}
CAPTURE_PLUGIN_CHOICES = ("gfxr", "patrace")
REPLAYER_PLUGIN_CHOICES = ("auto", "gfxr", "patrace")


class CLIError(RuntimeError):
    """Expected user-facing CLI error."""


def _print(message):
    print(message, flush=True)


def configure_command_loglevel(loglevel):
    global logger
    if not loglevel:
        return
    os.environ[LOG_LEVEL_ENV] = loglevel.upper()
    logger = setup_logger("traceui_cli")


def _ensure_dir(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def build_transfer_progress_logger():
    state = {"last_percent": -1}

    def _log_progress(percent, message):
        if percent is None:
            return
        if percent == 0 or percent == 100 or percent - state["last_percent"] >= 5:
            logger.debug("%s", message)
            state["last_percent"] = percent

    return _log_progress


def init_adb(device=None):
    adb = adblib.adb()
    devices = adb.init()
    if not devices:
        raise CLIError("No connected Android devices found.")
    if device:
        adb.select_device(device)
    elif adb.device is None:
        if len(adb.devices) > 1:
            raise CLIError(
                "Multiple devices connected. Use --device SERIAL to choose one."
            )
        adb.select_device(adb.devices[0])
    return adb


def load_plugins(adb):
    plugins = {}
    sys.path.insert(0, str(PLUGINS_PATH))
    try:
        for entry in os.listdir(PLUGINS_PATH):
            filename, ext = os.path.splitext(entry)
            if ext != ".py":
                continue
            module = importlib.import_module(filename)
            plugin = module.tracetool(adb)
            plugins[plugin.plugin_name] = plugin
    finally:
        sys.path.pop(0)
    return plugins


def normalize_cli_plugin_name(plugin_name):
    if plugin_name is None:
        return None
    return CLI_PLUGIN_ALIASES.get(plugin_name, plugin_name)


def reset_plugin_capture_config_to_defaults(plugin):
    if hasattr(plugin, "reset_capture_config_to_defaults"):
        plugin.reset_capture_config_to_defaults()

def build_capture_sample_config(plugins):
    sample_config = {
        "devicepaths": {},
        "plugin": {},
    }

    for plugin_name in sorted(plugins.keys()):
        plugin = plugins[plugin_name]
        if not hasattr(plugin, "get_capture_config_template"):
            continue
        reset_plugin_capture_config_to_defaults(plugin)

        template = plugin.get_capture_config_template()
        if not isinstance(template, dict):
            raise CLIError(f"Plugin '{plugin.plugin_name}' returned an invalid capture config template.")

        template_devicepaths = template.get("devicepaths", {})
        if not isinstance(template_devicepaths, dict):
            raise CLIError(f"Plugin '{plugin.plugin_name}' returned invalid devicepaths in capture config template.")
        for key, value in template_devicepaths.items():
            if key not in sample_config["devicepaths"]:
                sample_config["devicepaths"][key] = value

        template_plugins = template.get("plugin", {})
        if not isinstance(template_plugins, dict):
            raise CLIError(f"Plugin '{plugin.plugin_name}' returned an invalid plugin section in capture config template.")
        sample_config["plugin"].update(template_plugins)

    if not sample_config["plugin"]:
        raise CLIError("No plugins expose a capture config template.")

    return sample_config


def resolve_plugin(plugins, plugin_name=None, trace_path=None):
    plugin_name = normalize_cli_plugin_name(plugin_name)
    if plugin_name and plugin_name != "auto":
        if plugin_name not in plugins:
            raise CLIError(f"Unknown plugin '{plugin_name}'. Available: {sorted(plugins.keys())}")
        return plugins[plugin_name]

    if trace_path is None:
        raise CLIError("A trace path is required when plugin is set to auto.")

    suffix = Path(trace_path).suffix.lstrip(".")
    matches = [plugin for plugin in plugins.values() if getattr(plugin, "suffix", None) == suffix]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CLIError(f"No plugin found for trace suffix '.{suffix}'.")
    raise CLIError(f"Multiple plugins matched trace suffix '.{suffix}'.")


def _extract_application_label(adb, package_name):
    info, _ = adb.command(["dumpsys", "package", package_name], errors_handled_externally=True, print_command=False)
    for line in info.splitlines():
        stripped = line.strip()
        if stripped.startswith("application-label:"):
            return stripped.split(":", 1)[1].strip().strip("'")
        if stripped.startswith("application-label-"):
            return stripped.split(":", 1)[1].strip().strip("'")
    return None


def resolve_target_app(adb, target):
    target = target.strip()
    apps = adb.apps(all=True)
    logger.info("Searching for target app '%s' among %s installed packages.", target, len(apps))
    package_matches = [app["name"] for app in apps if target.lower() in app["name"].lower()]
    logger.info("Found %s package matches for target '%s'.", len(package_matches), target)
    if len(package_matches) == 1:
        return package_matches[0]

    preview = ", ".join(sorted(package_matches)[:10]) if package_matches else "none"
    raise CLIError(
        f"Could not uniquely resolve target '{target}'. Candidate packages: {preview}"
    )


def list_installed_packages(adb):
    stdout, _ = adb.command(["cmd", "package", "list", "packages"], errors_handled_externally=True)
    packages = []
    for line in stdout.splitlines():
        if line.startswith("package:"):
            packages.append(line.split(":", 1)[1].strip())
    return sorted(packages)


def launch_target_app(adb, package_name):
    adb.command(
        ["monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"],
        errors_handled_externally=True,
    )


def _print_error_lines(header, err_lines):
    if not err_lines:
        return
    logger.error(header)
    for line in err_lines:
        logger.error(line)


def _parse_plugin_logcat(plugin, mode, app=None):
    if not hasattr(plugin, "parse_logcat"):
        return []
    return plugin.parse_logcat(mode=mode, app=app)


def _raise_capture_failure(plugin, app, fallback_message):
    err_lines = _parse_plugin_logcat(plugin, mode="trace", app=app)
    _print_error_lines("Trace log warnings/errors:", err_lines)
    if err_lines:
        raise CLIError("Tracing was not succesful")
    raise CLIError(fallback_message)


def _ensure_capture_trace_exists(adb, plugin, app, remote_trace):
    if remote_trace is None:
        _raise_capture_failure(plugin, app, "No trace was captured. Try a different plugin.")

    _, ls_error = adb.command(["ls", str(remote_trace)], errors_handled_externally=True)
    if ls_error:
        _raise_capture_failure(plugin, app, "No trace was captured. Try a different plugin.")


def apply_plugin_config(plugin, config_path):
    if not hasattr(plugin, "load_capture_config"):
        raise CLIError(f"Plugin '{plugin.plugin_name}' does not support config files.")
    try:
        plugin.load_capture_config(config_path)
    except FileNotFoundError:
        raise CLIError(f"Config file not found: {config_path}")
    except ValueError as exc:
        raise CLIError(f"Invalid config file: {exc}")


def validate_local_trace(trace_path):
    trace_path = Path(trace_path)
    if not trace_path.is_file():
        raise CLIError(f"Trace file not found: {trace_path}")
    if trace_path.stat().st_size == 0:
        raise CLIError(f"Trace file is empty: {trace_path}")
    return trace_path


def prepare_remote_trace(adb, plugin, trace_path):
    trace_path = validate_local_trace(trace_path)
    remote_trace = plugin.sdcard_working_dir / trace_path.name
    adb.command(["mkdir", "-p", str(plugin.sdcard_working_dir)], True)
    adb.delete_file(remote_trace)
    progress_callback = build_transfer_progress_logger()
    if not adb.push(str(trace_path), str(plugin.sdcard_working_dir), track=False, progress_callback=progress_callback):
        raise CLIError(f"Failed to push trace to device: {trace_path}")
    return trace_path, remote_trace


def cleanup_replay_artifacts(adb, plugin, trace_on_device, screenshots):
    if not screenshots:
        return
    dir_prefix = f"{Path(trace_on_device).stem}_screenshot"
    screenshot_dir = plugin.sdcard_working_dir / dir_prefix
    adb.command(["rm", "-rf", str(screenshot_dir)], True)


def _ensure_remote_file_exists(adb, remote_path, description):
    _, ls_error = adb.command(["ls", str(remote_path)], errors_handled_externally=True)
    if ls_error:
        raise CLIError(f"{description} was not created on device: {remote_path}")


def collect_fastforward_output(adb, plugin, remote_output, outdir):
    outdir = Path(outdir)
    _ensure_dir(outdir)
    _ensure_remote_file_exists(adb, remote_output, "Fast-forward trace")

    if plugin.plugin_name == "gfxreconstruct":
        staging_dir = DEFAULT_OUTPUT_DIR
        _ensure_dir(staging_dir)
        progress_callback = build_transfer_progress_logger()
        if not adb.pull(str(remote_output), str(staging_dir), progress_callback=progress_callback):
            raise CLIError(f"Failed to pull fast-forward trace from device: {remote_output}")
        staged_trace = staging_dir / Path(remote_output).name
        optimized_trace = plugin.optimize_trace(str(staged_trace))
        if optimized_trace is None:
            final_local_path = outdir / staged_trace.name
            if staged_trace.resolve() != final_local_path.resolve():
                shutil.copy2(staged_trace, final_local_path)
            return final_local_path

        optimized_trace = Path(optimized_trace)
        final_local_path = outdir / optimized_trace.name
        if optimized_trace.resolve() != final_local_path.resolve():
            shutil.move(str(optimized_trace), str(final_local_path))
        return final_local_path

    final_local_path = outdir / Path(remote_output).name
    progress_callback = build_transfer_progress_logger()
    if not adb.pull(str(remote_output), str(outdir), progress_callback=progress_callback):
        raise CLIError(f"Failed to pull fast-forward trace from device: {remote_output}")
    return final_local_path


def finalize_capture_output(plugin, local_path, outdir):
    local_path = Path(local_path)
    outdir = Path(outdir)
    if plugin.plugin_name != "gfxreconstruct":
        return local_path

    optimized_trace = plugin.optimize_trace(str(local_path))
    if optimized_trace is None:
        return local_path

    optimized_trace = Path(optimized_trace)
    final_local_path = outdir / optimized_trace.name
    if optimized_trace.resolve() != final_local_path.resolve():
        shutil.move(str(optimized_trace), str(final_local_path))
    if local_path != final_local_path and local_path.exists():
        local_path.unlink()
    return final_local_path


def save_capture_session(session_path, data):
    session_path = Path(session_path)
    _ensure_dir(session_path.parent)
    with open(session_path, "w") as outfile:
        json.dump(data, outfile, indent=2)


def load_capture_session(session_path):
    session_path = Path(session_path)
    if not session_path.is_file():
        raise CLIError(f"Capture session file not found: {session_path}")
    with open(session_path, "r") as infile:
        return json.load(infile)


def remove_capture_session(session_path):
    session_path = Path(session_path)
    if session_path.exists():
        session_path.unlink()


def write_patrace_replay_args(adb, plugin, data):
    _ensure_dir(DEFAULT_OUTPUT_DIR)
    local_args_path = DEFAULT_OUTPUT_DIR / "replay_args.json"
    with open(local_args_path, "w") as outfile:
        json.dump(data, outfile, indent=2)
    adb.command(["mkdir", "-p", str(plugin.sdcard_working_dir)], True)
    adb.delete_file(plugin.sdcard_working_dir / local_args_path.name)
    if not adb.push(str(local_args_path), str(plugin.sdcard_working_dir), track=False):
        raise CLIError("Failed to push patrace replay_args.json to device.")


def start_replay_process(adb, plugin, cmd):
    process_name = plugin.replayer["name"]
    if "gfxreconstruct" in process_name:
        logger.debug("Launching replay command: %s", " ".join([str(x) for x in cmd]))
        subprocess.run(" ".join([str(x) for x in cmd]), shell=True, capture_output=False)
    else:
        adb.command(cmd)
        logger.debug("Launching replay command: %s", cmd)

    logger.info("Replay started ...")
    time.sleep(0.1)
    stdout, _ = adb.command([f"ps -A | grep {process_name}"], print_command=False)
    while process_name in stdout:
        time.sleep(0.5)
        stdout, _ = adb.command([f"ps -A | grep {process_name}"], print_command=False)


def _get_screenshot_paths(adb, base_dir, prefix):
    paths, _ = adb.command([f"ls {base_dir} | grep {prefix}"], errors_handled_externally=True)
    if not paths:
        return []
    return [f"{base_dir}/{item}" for item in paths.split()]


def _extract_patrace_frame_num(remote_path):
    frame_suffix = Path(remote_path).stem.split("frame_", 1)[-1]
    match = re.match(r"(\d+)(?:_|$)", frame_suffix)
    if not match:
        return None
    return int(match.group(1))


def parse_frame_list(raw_value):
    if raw_value is None:
        return None

    parts = [part.strip() for part in raw_value.split(",")]
    if not parts or any(not part for part in parts):
        raise CLIError("Frames must be a comma-separated list of non-negative integers.")

    frames = []
    seen = set()
    for part in parts:
        try:
            frame = int(part)
        except ValueError:
            raise CLIError("Frames must be a comma-separated list of non-negative integers.")
        if frame < 0:
            raise CLIError("Frames must be a comma-separated list of non-negative integers.")
        if frame in seen:
            continue
        seen.add(frame)
        frames.append(frame)

    return frames


def collect_replay_outputs(adb, plugin, trace_on_device, screenshots, interval, outdir):
    results = {"screenshots": []}
    outdir = Path(outdir)
    _ensure_dir(outdir)

    if screenshots:
        dir_prefix = f"{Path(trace_on_device).stem}_screenshot"
        screenshot_dir = plugin.sdcard_working_dir / dir_prefix
        screenshot_prefix = f"{dir_prefix}_frame_"
        screenshot_paths = _get_screenshot_paths(adb, screenshot_dir, screenshot_prefix)
        if plugin.plugin_name == "patrace":
            for remote_path in screenshot_paths:
                frame_num = _extract_patrace_frame_num(remote_path)
                if frame_num is None:
                    logger.warning("Skipping unexpected patrace screenshot name: %s", remote_path)
                    continue
                normalized_path = f"{screenshot_dir}/{screenshot_prefix}{frame_num}.png"
                if remote_path == normalized_path:
                    continue
                adb.command([f"mv {remote_path} {normalized_path}"], True)
            screenshot_paths = _get_screenshot_paths(adb, screenshot_dir, screenshot_prefix)
        logger.info("Pulling screenshots from device")
        for remote_path in screenshot_paths:
            progress_callback = build_transfer_progress_logger()
            if not adb.pull(remote_path, str(outdir), progress_callback=progress_callback):
                raise CLIError(f"Failed to pull screenshot from device: {remote_path}")
            results["screenshots"].append(str(outdir / Path(remote_path).name))

    return results


def execute_replay_run(adb, plugin, remote_trace, outdir, screenshot_mode=False, interval=10, from_frame=None, to_frame=None):
    adb.clear_logcat()
    cleanup_replay_artifacts(adb, plugin, remote_trace, screenshot_mode)
    plugin.replay_setup()
    cmd, data = plugin.replay_start(
        remote_trace,
        screenshot=screenshot_mode,
        interval=interval,
        from_frame=from_frame,
        to_frame=to_frame,
        extra_args=list(getattr(plugin, "extra_args", [])),
    )
    if cmd is None:
        raise CLIError("Replay setup failed before launching replay.")

    if plugin.plugin_name == "patrace":
        write_patrace_replay_args(adb, plugin, data)

    try:
        start_replay_process(adb, plugin, cmd)
        results = collect_replay_outputs(adb, plugin, remote_trace, screenshot_mode, interval, outdir)
        err_lines = plugin.parse_logcat(mode="replay")
    finally:
        plugin.replay_reset_device()

    return results, err_lines


def _extract_screenshot_sort_frame_num(path):
    match = re.search(r"frame_(\d+)", Path(path).stem)
    if not match:
        return None
    return int(match.group(1))


def stage_deterministic_frames(results, requested_frames, outdir, run_label, trace_stem):
    screenshots = results.get("screenshots", [])
    if len(screenshots) != len(requested_frames):
        raise CLIError(
            f"Expected {len(requested_frames)} screenshot(s) during {run_label}, found {len(screenshots)}."
        )

    ordered_screenshots = []
    for source in screenshots:
        source_path = Path(source)
        if not source_path.is_file():
            raise CLIError(f"Replay screenshot missing for {run_label}: {source_path}")
        sort_frame = _extract_screenshot_sort_frame_num(source_path)
        ordered_screenshots.append(((sort_frame is None, sort_frame, source_path.name), source_path))
    ordered_screenshots.sort(key=lambda item: item[0])

    staged = {}
    for frame_number, (_, source_path) in zip(requested_frames, ordered_screenshots):
        target_path = outdir / f"{trace_stem}_compare_{run_label}_frame_{frame_number}.png"
        _ensure_dir(target_path.parent)
        shutil.move(str(source_path), str(target_path))
        staged[frame_number] = target_path
    return staged


def compare_replay_frames(frame_number, first_image, second_image, diff_image):
    compare_cmd = [
        "compare",
        "-alpha",
        "off",
        "-metric",
        "RMSE",
        str(first_image),
        str(second_image),
        str(diff_image),
    ]
    try:
        process = subprocess.run(compare_cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise CLIError("ImageMagick 'compare' not found. Install ImageMagick to use --deterministic_check.")

    stderr = process.stderr.strip()
    if "compare: not found" in stderr:
        raise CLIError("ImageMagick 'compare' not found. Install ImageMagick to use --deterministic_check.")
    if process.returncode not in (0, 1):
        detail = stderr or process.stdout.strip() or f"exit code {process.returncode}"
        raise CLIError(f"Failed to compare frame {frame_number}: {detail}")

    rmse = stderr.splitlines()[-1].strip() if stderr else "0"
    first_token = rmse.split()[0] if rmse else "0"
    frames_differ = first_token != "0"
    return frames_differ, rmse


def handle_capture_setup(args):
    configure_command_loglevel(args.loglevel)
    adb = init_adb(args.device)
    plugins = load_plugins(adb)
    plugin = resolve_plugin(plugins, args.plugin)
    plugin.adb = adb
    reset_plugin_capture_config_to_defaults(plugin)

    if args.config:
        apply_plugin_config(plugin, args.config)

    resolved_target = resolve_target_app(adb, args.app)
    logger.debug("Using device: %s", adb.device)
    logger.debug("Using plugin: %s", plugin.plugin_name)
    logger.info("Resolved target: %s", resolved_target)

    adb.clear_logcat()
    plugin.trace_setup_device(resolved_target)
    if hasattr(plugin, "trace_setup_check") and not plugin.trace_setup_check(resolved_target):
        if plugin.plugin_name == "gfxreconstruct":
            raise CLIError(
                "Trace setup verification failed after the rooted and non-root gfxr setup attempts. "
                "For the non-root fallback, the target app must be debuggable and support run-as."
            )
        raise CLIError("Trace setup verification failed.")

    session_data = {
        "plugin": plugin.plugin_name,
        "device": adb.device,
        "resolved_target": resolved_target,
        "requested_app": args.app,
        "config_path": str(args.config) if args.config else None,
        "outdir": str(DEFAULT_OUTPUT_DIR),
        "plugin_state": plugin.export_capture_session_state() if hasattr(plugin, "export_capture_session_state") else {},
    }
    save_capture_session(args.state_file, session_data)
    logger.info("Capture armed. Session stored at: %s", args.state_file)
    if args.launch_app:
        launch_target_app(adb, resolved_target)
        logger.info("Started target app: %s", resolved_target)
    else:
        logger.info("Launch the target app manually, then run capture stop to fetch the trace.")
    return 0


def handle_capture_stop(args):
    session = load_capture_session(args.state_file)
    session_plugin = session.get("plugin")
    if not session_plugin:
        raise CLIError("Capture session is missing the plugin name.")

    adb = init_adb(args.device)
    plugins = load_plugins(adb)
    plugin = resolve_plugin(plugins, session_plugin)
    plugin.adb = adb

    if session.get("device") and session["device"] != adb.device:
        raise CLIError(
            f"Session device '{session['device']}' does not match active device '{adb.device}'."
        )

    resolved_target = session["resolved_target"]
    if args.app:
        requested_target = resolve_target_app(adb, args.app)
        if requested_target != resolved_target:
            raise CLIError(
                f"Resolved target '{requested_target}' does not match session target '{resolved_target}'."
            )

    if hasattr(plugin, "import_capture_session_state"):
        plugin.import_capture_session_state(session.get("plugin_state", {}))

    outdir = Path(args.outdir or session.get("outdir") or DEFAULT_OUTPUT_DIR)
    _ensure_dir(outdir)
    logger.info("Stopping capture for: %s", resolved_target)

    remote_trace = None
    trace_stop_handle_transfers = getattr(plugin, "trace_stop_handle_transfers", None)
    if trace_stop_handle_transfers is not None:
        plugin.trace_stop_handle_transfers = False
    try:
        remote_trace = plugin.trace_stop(resolved_target)
        _ensure_capture_trace_exists(adb, plugin, resolved_target, remote_trace)
        logger.info("Pulling trace from device")
        progress_callback = build_transfer_progress_logger()
        if not adb.pull(str(remote_trace), str(outdir), progress_callback=progress_callback):
            raise CLIError(f"Failed to pull trace from device: {remote_trace}")
    finally:
        if trace_stop_handle_transfers is not None:
            plugin.trace_stop_handle_transfers = trace_stop_handle_transfers
        plugin.trace_reset_device()

    local_path = outdir / Path(remote_trace).name
    local_path = finalize_capture_output(plugin, local_path, outdir)
    remove_capture_session(args.state_file)
    logger.info("Trace pulled to: %s", local_path)

    return 0


def handle_capture_list_packages(args):
    adb = init_adb(args.device)
    packages = list_installed_packages(adb)
    for package in packages:
        _print(package)
    return 0


def handle_capture_sample_config(args):
    plugins = load_plugins(None)
    sample_config = build_capture_sample_config(plugins)
    sample_json = json.dumps(sample_config, indent=2)

    if args.output:
        output_path = Path(args.output)
        _ensure_dir(output_path.parent)
        output_path.write_text(sample_json + "\n")
        logger.info("Wrote sample config to: %s", output_path)
    else:
        _print(sample_json)

    return 0


def handle_replay(args):
    configure_command_loglevel(args.loglevel)
    adb = init_adb(args.device)
    plugins = load_plugins(adb)
    trace_path = validate_local_trace(args.trace)

    plugin = resolve_plugin(plugins, trace_path=trace_path)
    plugin.adb = adb
    reset_plugin_capture_config_to_defaults(plugin)
    if args.config:
        apply_plugin_config(plugin, args.config)
    trace_stem = trace_path.stem
    outdir = Path(args.outdir or DEFAULT_OUTPUT_DIR)
    _ensure_dir(outdir)
    selected_frames = parse_frame_list(args.frames)
    deterministic_frames = sorted(selected_frames) if selected_frames is not None else None
    if args.interval is not None and args.interval < 0:
        raise CLIError("Screenshot interval must be >= 0.")
    if args.interval is not None and not args.screenshots:
        raise CLIError("--interval requires --screenshots.")
    if selected_frames is not None and args.interval is not None:
        raise CLIError("--frames cannot be used with --interval.")
    if args.deterministic_check:
        if deterministic_frames is None:
            raise CLIError("--deterministic_check requires --frames.")
        if args.interval is not None:
            raise CLIError("--interval cannot be used with --deterministic_check.")
    if args.end_frame is not None:
        if args.end_frame < 0:
            raise CLIError("Replay end frame must be >= 0.")
        if selected_frames is not None:
            out_of_range_frames = [frame for frame in selected_frames if frame > args.end_frame]
            if out_of_range_frames:
                raise CLIError("--frames cannot include values greater than --end-frame.")

    _, remote_trace = prepare_remote_trace(adb, plugin, trace_path)

    logger.debug("Using device: %s", adb.device)
    logger.debug("Using plugin: %s", plugin.plugin_name)
    logger.debug("Trace on device: %s", remote_trace)

    if args.deterministic_check:
        compare_run1_dir = outdir / ".compare_run1"
        compare_run2_dir = outdir / ".compare_run2"
        try:
            logger.info("Capturing frames %s from replay run 1.", deterministic_frames)
            run1_results, run1_errors = execute_replay_run(
                adb,
                plugin,
                remote_trace,
                compare_run1_dir,
                screenshot_mode="selecting_frames",
                from_frame=deterministic_frames,
                to_frame=args.end_frame,
            )
            if run1_errors:
                _print_error_lines("Replay reported errors on run 1:", run1_errors)
                return 1
            staged_run1 = stage_deterministic_frames(run1_results, deterministic_frames, outdir, "run1", trace_stem)

            logger.info("Capturing frames %s from replay run 2.", deterministic_frames)
            run2_results, run2_errors = execute_replay_run(
                adb,
                plugin,
                remote_trace,
                compare_run2_dir,
                screenshot_mode="selecting_frames",
                from_frame=deterministic_frames,
                to_frame=args.end_frame,
            )
            if run2_errors:
                _print_error_lines("Replay reported errors on run 2:", run2_errors)
                return 1
            staged_run2 = stage_deterministic_frames(run2_results, deterministic_frames, outdir, "run2", trace_stem)
        finally:
            shutil.rmtree(compare_run1_dir, ignore_errors=True)
            shutil.rmtree(compare_run2_dir, ignore_errors=True)

        differing_frames = []
        for frame_number in deterministic_frames:
            first_image = staged_run1[frame_number]
            second_image = staged_run2[frame_number]
            diff_image = outdir / f"{trace_stem}_diff_frame_{frame_number}.png"
            frames_differ, rmse = compare_replay_frames(frame_number, first_image, second_image, diff_image)
            logger.debug("Run 1 frame saved to: %s", first_image)
            logger.debug("Run 2 frame saved to: %s", second_image)
            logger.debug("Frame %s RMSE: %s", frame_number, rmse)
            if frames_differ:
                differing_frames.append(frame_number)
                logger.info("Frame %s differed between replay runs. Diff image: %s", frame_number, diff_image)
            else:
                logger.info("Frame %s matched between replay runs. Diff image: %s", frame_number, diff_image)

        if differing_frames:
            logger.error("Deterministic check failed for frame(s): %s", differing_frames)
            return 1

        logger.info("Deterministic check passed.")
        return 0

    if selected_frames is not None:
        screenshots_mode = "selecting_frames"
    elif args.screenshots:
        screenshots_mode = "interval"
    else:
        screenshots_mode = False
    replay_interval = args.interval if args.interval is not None else 10
    results, err_lines = execute_replay_run(
        adb,
        plugin,
        remote_trace,
        outdir,
        screenshot_mode=screenshots_mode,
        interval=replay_interval,
        from_frame=selected_frames,
        to_frame=args.end_frame,
    )

    if results["screenshots"]:
        logger.info("Pulled %s screenshot(s) to: %s", len(results["screenshots"]), outdir)
    else:
        logger.info("Replay finished.")

    if err_lines:
        _print_error_lines("Replay reported errors:", err_lines)
        return 1

    return 0


def handle_fastforward(args):
    configure_command_loglevel(args.loglevel)
    if args.start_frame < 0:
        raise CLIError("Fast-forward start frame must be >= 0.")
    if args.end_frame is not None and args.end_frame < args.start_frame:
        raise CLIError("Fast-forward end frame must be >= start frame.")

    adb = init_adb(args.device)
    plugins = load_plugins(adb)
    trace_path = validate_local_trace(args.trace)
    plugin = resolve_plugin(plugins, args.plugin, trace_path)
    plugin.adb = adb
    reset_plugin_capture_config_to_defaults(plugin)
    if args.config:
        apply_plugin_config(plugin, args.config)

    fastforward_plugin = plugins.get("fastforward")
    if fastforward_plugin is None or not hasattr(fastforward_plugin, "replay_start_fastforward"):
        raise CLIError("Fastforward plugin is not available.")
    fastforward_plugin.adb = adb

    outdir = Path(args.outdir or DEFAULT_OUTPUT_DIR)
    _ensure_dir(outdir)
    _, remote_trace = prepare_remote_trace(adb, plugin, trace_path)
    logger.info("Trace pushed to device. Generating fast-forward trace from frame %s.", args.start_frame)

    logger.debug("Using device: %s", adb.device)
    logger.debug("Using plugin: %s", plugin.plugin_name)
    logger.debug("Trace on device: %s", remote_trace)
    logger.debug("Generating fast-forward trace from frame: %s", args.start_frame)

    adb.clear_logcat()
    plugin.replay_setup()

    remote_output = None
    local_output = None
    try:
        cmd, remote_output = fastforward_plugin.replay_start_fastforward(
            remote_trace,
            plugin,
            from_frame=args.start_frame,
            to_frame=args.end_frame,
        )
        if cmd is None or remote_output is None:
            raise CLIError("Fast-forward setup failed before launch.")

        start_replay_process(adb, plugin, cmd)
        local_output = collect_fastforward_output(adb, plugin, remote_output, outdir)
        err_lines = plugin.parse_logcat(mode="replay")
    finally:
        plugin.replay_reset_device()

    if local_output is not None:
        logger.info("Fast-forward trace saved to: %s", local_output)

    if err_lines:
        _print_error_lines("Fast-forward reported errors:", err_lines)
        return 1

    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="traceui-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser("capture", help="Capture workflow commands.")
    capture_subparsers = capture_parser.add_subparsers(dest="capture_command", required=True)

    capture_setup = capture_subparsers.add_parser("setup")
    capture_setup.add_argument(
        "--plugin",
        required=True,
        choices=CAPTURE_PLUGIN_CHOICES,
        help="Capture plugin."
    )
    capture_setup.add_argument("--app", required=True, help="Target Android package or app name.")
    capture_setup.add_argument("-c", "--config", type=Path, help="Plugin-scoped capture config JSON.")
    capture_setup.add_argument("-d", "--device", help="ADB device serial.")
    capture_setup.add_argument(
        "--loglevel",
        choices=("debug", "info", "warning", "error", "critical"),
        help="Override CLI/plugin log level for this command.",
    )
    capture_setup.add_argument(
        "--launch-app",
        action="store_true",
        help="Launch the target app after capture setup completes.",
    )
    capture_setup.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_SESSION_FILE,
        help="Path to the capture session state file shared between setup and stop.",
    )
    capture_setup.set_defaults(handler=handle_capture_setup)

    capture_stop = capture_subparsers.add_parser("stop")
    capture_stop.add_argument("--app", help="Optional package or app name for validation.")
    capture_stop.add_argument("-d", "--device", help="ADB device serial.")
    capture_stop.add_argument("-o", "--outdir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Local output directory.")
    capture_stop.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_SESSION_FILE,
        help="Path to the capture session state file created by capture setup.",
    )
    capture_stop.set_defaults(handler=handle_capture_stop)

    capture_list_packages = capture_subparsers.add_parser("list-packages")
    capture_list_packages.add_argument("-d", "--device", help="ADB device serial.")
    capture_list_packages.set_defaults(handler=handle_capture_list_packages)

    capture_sample_config = capture_subparsers.add_parser("sample-config", help="Print a sample capture config JSON.")
    capture_sample_config.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Optional path to write the sample config JSON.",
    )
    capture_sample_config.set_defaults(handler=handle_capture_sample_config)

    sample_config_parser = subparsers.add_parser("sample-config", help="Print a sample capture config JSON.")
    sample_config_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Optional path to write the sample config JSON.",
    )
    sample_config_parser.set_defaults(handler=handle_capture_sample_config)

    replay_parser = subparsers.add_parser("replay", help="Replay a local trace on device.")
    replay_parser.add_argument("trace", type=Path, help="Local trace path.")
    replay_parser.add_argument("--device", help="ADB device serial.")
    replay_parser.add_argument("-c", "--config", type=Path, help="Config JSON used to override device paths.")
    replay_parser.add_argument(
        "--loglevel",
        choices=("debug", "info", "warning", "error", "critical"),
        help="Override CLI/plugin log level for this command.",
    )
    replay_parser.add_argument("-s", "--screenshots", action="store_true", help="Capture screenshots during replay.")
    replay_parser.add_argument(
        "-d",
        "--deterministic_check",
        action="store_true",
        help="Replay twice using --frames and compare the captured screenshots.",
    )
    replay_parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=None,
        help="Screenshot interval when --screenshots is enabled; requires --screenshots, defaults to 10, and 0 disables capture within screenshot mode.",
    )
    replay_parser.add_argument(
        "-f",
        "--frames",
        help="Comma-separated frame numbers to capture snapshots for. Cannot be used with --interval.",
    )
    replay_parser.add_argument(
        "-ef",
        "--end-frame",
        type=int,
        dest="end_frame",
        help="Optional last frame to replay; omit to replay to the end.",
    )
    replay_parser.add_argument("-o", "--outdir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Local output directory.")
    replay_parser.set_defaults(handler=handle_replay)

    fastforward_parser = subparsers.add_parser("fastforward", help="Generate a fast-forward trace.")
    fastforward_parser.add_argument("trace", type=Path, help="Local trace path.")
    fastforward_parser.add_argument("--plugin", default="auto", choices=REPLAYER_PLUGIN_CHOICES, help="Plugin name or 'auto'.")
    fastforward_parser.add_argument("-d", "--device", help="ADB device serial.")
    fastforward_parser.add_argument("-c", "--config", type=Path, help="Config JSON used to override device paths.")
    fastforward_parser.add_argument(
        "--loglevel",
        choices=("debug", "info", "warning", "error", "critical"),
        help="Override CLI/plugin log level for this command.",
    )
    fastforward_parser.add_argument(
        "-sf",
        "--start-frame",
        required=True,
        type=int,
        help="First frame to keep in the fast-forward trace.",
    )
    fastforward_parser.add_argument(
        "-ef",
        "--end-frame",
        type=int,
        help="Optional last frame to keep; omit to run to the end.",
    )
    fastforward_parser.add_argument("-o", "--outdir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Local output directory.")
    fastforward_parser.set_defaults(handler=handle_fastforward)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except CLIError as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        logger.exception("CLI command failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
