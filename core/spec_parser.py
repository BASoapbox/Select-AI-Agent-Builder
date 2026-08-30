
import json, re
def parse_spec(text):
    m = re.search(r"<SPEC>(.*?)</SPEC>", text, re.S)
    if not m:
        raise Exception("No SPEC block")
    return json.loads(m.group(1))
