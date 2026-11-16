"""Inspect the auth object the library creates."""
from dotenv import load_dotenv
load_dotenv(override=True)
import os

from cwsandbox._auth import _resolve_wandb_auth, AuthStrategy, resolve_auth

# Get the resolved auth
auth = resolve_auth(AuthStrategy.WANDB)
print(f"resolve_auth type: {type(auth).__name__}")
print(f"resolve_auth attrs: {[a for a in dir(auth) if not a.startswith('_')]}", flush=True)

# Check if it's a string, dict, or object
if hasattr(auth, 'token'):
    print(f"  token: {auth.token[:20]}...{auth.token[-4:]}", flush=True)
if hasattr(auth, 'headers'):
    for k, v in auth.headers.items():
        val = str(v)
        if len(val) > 40: val = val[:20] + "..." + val[-4:]
        print(f"  header {k}: {val}", flush=True)
if hasattr(auth, 'url'):
    print(f"  url: {auth.url}", flush=True)
if isinstance(auth, str):
    print(f"  is string: {auth[:20]}...{auth[-4:]}", flush=True)

# Also check the init signature of cwsandbox Sandbox.run
import inspect
from cwsandbox import Sandbox
sig = inspect.signature(Sandbox.run)
for name, param in sig.parameters.items():
    print(f"  Sandbox.run({name}) default: {param.default if param.default is not inspect.Parameter.empty else 'required'}", flush=True)
