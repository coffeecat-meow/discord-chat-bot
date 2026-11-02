import sys
from dotenv import dotenv_values
from sayuki_bot.config import load_config
from sayuki_bot.llm.routing import route, validate_routes
env = dotenv_values(sys.argv[1])
for path in sys.argv[2:]:
    cfg = load_config(path); validate_routes(cfg)
    print(f"== {path}")
    print(f"{'role':9} {'slot':8} {'enabled':7} {'api_format':10} {'api_key_env':18} {'key':7} base_url / model")
    for role in ("chat","attention","memory","vision","task"):
        for slot in ("primary","fallback"):
            r = route(cfg, role, slot)
            k = "set" if env.get(r.api_key_env) else ("empty" if r.api_key_env in env else "missing")
            print(f"{role:9} {slot:8} {str(r.enabled):7} {r.api_format:10} {r.api_key_env:18} {k:7} {r.base_url} / {r.model}")
