import json
import subprocess
import sys
from pathlib import Path
from rosbag_timeline.demo import generate


def run(*args):
    return subprocess.run([sys.executable, "-m", "rosbag_timeline", *map(str,args)], capture_output=True, text=True)


def test_cli_report_and_bad_window(tmp_path):
    path = generate(tmp_path / "demo.mcap", 60)
    output = tmp_path / "report"
    r = run("stats",path,"--output",output,"--cache-dir",tmp_path/'cache',"--search","PointCloud2","--json-only")
    assert r.returncode == 0, r.stderr
    assert (output / "summary.csv").exists()
    result = json.loads((output / "statistics.json").read_text())
    assert len(result["topics"]) == 1
    assert result["topics"][0]["message_count"] == 20
    r = run("stats",path,"--output",output,"--start","2","--end","1")
    assert r.returncode == 2
    assert "start < end" in r.stderr


def test_cli_info_custom_types(tmp_path):
    path = generate(tmp_path / "demo.mcap", 60)
    r = run("info",path,"--cache-dir",tmp_path/'cache')
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    assert info['topics'][1]['type'] == 'demo_msgs/msg/StampedValue'
    assert info['topics'][1]['stamps'] == ['header.stamp']
