"""Manage this node: serve, local users and local personal tokens.

Account commands select the database through --config or CONTAINERPROXY_CONFIG;
they work on either role, without propagating credentials to any peer. A live
server notices direct CLI changes after its short credential-cache expiry.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time

import uvicorn

from . import __version__
from .api import create_app
from .auth import ROLES, AccountError, create_token, create_user, update_user
from .config import load_config
from .store import Store


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg (+ exc)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(fmt: str, verbose: bool) -> None:
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # uvicorn's loggers propagate to root (log_config=None below), so its
    # server and access lines use the same format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise AccountError("passwords do not match")
    return first


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _accounts(args, cfg_path: str | None) -> int:
    config = load_config(cfg_path)
    reserved = config.push_username if config.push_password else None
    store = Store(config.storage_path)
    try:
        store.initialize_isolation()
        if args.cmd == "user":
            if args.action == "add":
                create_user(store, args.username, args.role, _read_password(args.password_stdin),
                            reserved=reserved)
                print(f"created user {args.username} ({args.role})")
            elif args.action == "list":
                users = store.list_users()
                if not users:
                    print("no users")
                for u in users:
                    state = "disabled" if u["disabled"] else "active"
                    print(f"{u['username']:<24} {u['role']:<7} {u['source']:<6} {state}")
            elif args.action == "set":
                password = _read_password(args.password_stdin) if args.password else None
                disabled = {"disable": True, "enable": False}.get(args.state)
                if args.role is None and disabled is None and password is None:
                    raise AccountError("nothing to change: pass --role, --disable/--enable or --password")
                u = update_user(store, args.username, role=args.role, disabled=disabled,
                                password=password)
                print(f"updated user {u['username']} ({u['role']}, "
                      f"{'disabled' if u['disabled'] else 'active'})")
            elif args.action == "delete":
                if not store.delete_user(args.username):
                    raise AccountError(f"no user {args.username!r}", 404)
                print(f"deleted user {args.username} and their tokens")
        else:
            if args.action == "create":
                full, row = create_token(store, args.username, args.name, args.expires_days)
                print(f"token {row['id']} for {args.username} "
                      f"(expires {_fmt_time(row['expires_at']) if row['expires_at'] else 'never'}):")
                print(full)
                print("It is shown only once. Use it as the password for "
                      "`docker login` / `helm registry login`.", file=sys.stderr)
            elif args.action == "list":
                tokens = store.list_tokens(args.username)
                if not tokens:
                    print("no tokens")
                for t in tokens:
                    print(f"{t['id']}  {t['username']:<20} {t['name']:<24} "
                          f"expires {_fmt_time(t['expires_at'])}  last used {_fmt_time(t['last_used_at'])}")
            elif args.action == "revoke":
                if not store.delete_token(args.token_id):
                    raise AccountError(f"no token {args.token_id!r}", 404)
                print(f"revoked token {args.token_id}")
    except AccountError as e:
        print(f"containerproxy: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def _add_config_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-c", "--config",
        help="path to containerproxy.toml (default: $CONTAINERPROXY_CONFIG; env-only also works)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="containerproxy",
        description="ContainerProxy — primary/replica pull-through container registry",
    )
    parser.add_argument("--version", action="version", version=f"containerproxy {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run a containerproxy node")
    serve.add_argument(
        "-c", "--config",
        help="path to containerproxy.toml (default: $CONTAINERPROXY_CONFIG; env-only also works)",
    )
    serve.add_argument("--host", help="override listen address")
    serve.add_argument("--port", type=int, help="override listen port")
    serve.add_argument("--role", choices=["primary", "replica"], help="override configured role")
    serve.add_argument(
        "--log-format", choices=["text", "json"],
        default=os.environ.get("CONTAINERPROXY_LOG_FORMAT") or "text",
        help="log format (default: $CONTAINERPROXY_LOG_FORMAT or text)",
    )
    serve.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    user = sub.add_parser("user", help="manage users on this node")
    user_sub = user.add_subparsers(dest="action", required=True)
    u_add = user_sub.add_parser("add", help="create a local user")
    u_add.add_argument("username")
    u_add.add_argument("--role", choices=ROLES, required=True)
    u_add.add_argument("--password-stdin", action="store_true",
                       help="read the password from stdin instead of prompting")
    u_list = user_sub.add_parser("list", help="list users")
    u_set = user_sub.add_parser("set", help="change a user's role, state or password")
    u_set.add_argument("username")
    u_set.add_argument("--role", choices=ROLES)
    state = u_set.add_mutually_exclusive_group()
    state.add_argument("--disable", dest="state", action="store_const", const="disable")
    state.add_argument("--enable", dest="state", action="store_const", const="enable")
    u_set.add_argument("--password", action="store_true", help="set a new password")
    u_set.add_argument("--password-stdin", action="store_true",
                       help="with --password: read it from stdin")
    u_del = user_sub.add_parser("delete", help="delete a user and their tokens")
    u_del.add_argument("username")

    token = sub.add_parser("token", help="manage access tokens on this node")
    token_sub = token.add_subparsers(dest="action", required=True)
    t_create = token_sub.add_parser("create", help="create a token for a user")
    t_create.add_argument("username")
    t_create.add_argument("--name", required=True, help="what the token is for, e.g. ci-runner")
    t_create.add_argument("--expires-days", type=float)
    t_list = token_sub.add_parser("list", help="list tokens (of one user, or all)")
    t_list.add_argument("username", nargs="?")
    t_revoke = token_sub.add_parser("revoke", help="revoke a token")
    t_revoke.add_argument("token_id")
    for p in (u_add, u_list, u_set, u_del, t_create, t_list, t_revoke):
        _add_config_arg(p)

    args = parser.parse_args(argv)
    if args.cmd in ("user", "token"):
        cfg_path = args.config or os.environ.get("CONTAINERPROXY_CONFIG")
        if cfg_path and not os.path.exists(cfg_path):
            if args.config:
                parser.error(f"config file not found: {cfg_path}")
            cfg_path = None
        try:
            return _accounts(args, cfg_path)
        except (ValueError, OSError) as e:
            print(f"containerproxy: configuration error: {e}", file=sys.stderr)
            return 2
    if args.log_format not in ("text", "json"):
        parser.error(f"invalid log format {args.log_format!r} (text or json)")
    setup_logging(args.log_format, args.verbose)

    if args.cmd == "serve":
        cfg_path = args.config or os.environ.get("CONTAINERPROXY_CONFIG")
        if cfg_path and not os.path.exists(cfg_path):
            if args.config:
                parser.error(f"config file not found: {cfg_path}")
            cfg_path = None  # env-only mode: CONTAINERPROXY_CONFIG default absent
        try:
            # CLI overrides go through the same validation as file/env values.
            config = load_config(cfg_path, role=args.role, listen=args.host, port=args.port)
            app = create_app(config)
        except (ValueError, OSError) as e:
            print(f"containerproxy: configuration error: {e}", file=sys.stderr)
            return 2
        uvicorn.run(
            app, host=config.listen, port=config.port, log_config=None,
            log_level="debug" if args.verbose else "info",
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
