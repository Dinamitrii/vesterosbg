import os, sys, re, importlib
root = "/home/dinamitrii/PycharmProjects/vesterosbg"
sys.path.insert(0, root)
env_path = os.path.join(root, ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
import app as app_module
app = app_module.app
print("IMPORTED OK")
print("TEMPLATE_FOLDER:", app.template_folder, "STATIC_FOLDER:", app.static_folder)
print("\n=== ALL URL RULES ===")
for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
    methods = sorted(str(m) for m in rule.methods)
    print(f"{rule.rule:45s} {methods}  {rule.endpoint}")
print("\n=== TEMPLATE presence check ===")
template_dir = os.path.join(root, "templates")
for tmpl in ["index.html", "gallery.html", "contact.html", "admin.html", "admin_client.html", "login.html", "in-dex.html", "base.html"]:
    p = os.path.join(template_dir, tmpl)
    print(f"  {'OK ' if os.path.exists(p) else 'MISSING'} {tmpl}")
print("\n=== URL_FOR resolve check (endpoints referenced in templates) ===")
refs = {}
for f in os.listdir(template_dir):
    if f.endswith(".html"):
        txt = open(os.path.join(template_dir, f), encoding="utf-8", errors="ignore").read()
        for m in re.findall(r"url_for\(\s*'([^']+)'", txt):
            refs.setdefault(m, []).append(f)
for ep in sorted(refs):
    try:
        with app.test_request_context():
            app.url_for(ep)
        print(f"  OK   {ep:25s} files={refs[ep]}")
    except Exception as e:
        print(f"  FAIL {ep:25s} files={refs[ep]} error={e}")
