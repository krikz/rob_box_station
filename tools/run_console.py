import sys, runpy, os
B = "/data/opt/rbx"
sys.path[:0] = [B + "/site", B + "/core", B + "/core/scripts/dialog"] + [B + "/core/src/" + p for p in
               ("rob_box_core", "rob_box_dialog", "rob_box_harness", "rob_box_llm", "rob_box_voice", "rob_box_mcp_tools")]
os.chdir(B + "/core")
sys.argv = ["scripts/dialogue/chat.py"] + sys.argv[1:]
runpy.run_path(B + "/core/scripts/dialogue/chat.py", run_name="__main__")
