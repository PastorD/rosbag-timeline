"""Optional ROS-side graph snapshot. The reader/viewer never imports rclpy."""
import json
import time
from pathlib import Path


def capture(output, wait=3.0, recorder="/bag_recorder"):
    try:
        import rclpy
    except ImportError as exc:
        raise ValueError("capture-graph runs in a sourced ROS 2 environment with rclpy; viewing bags does not require ROS") from exc
    rclpy.init()
    node = rclpy.create_node("rosbag_timeline_graph_capture")
    def endpoint(e):
        return {"node": "/" + "/".join(x.strip("/") for x in [e.node_namespace, e.node_name] if x.strip("/")),
                "gid": bytes(e.endpoint_gid).hex(), "type": e.topic_type}
    try:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        result = {"recorder": recorder, "captured_system_ns": str(time.time_ns()), "topics": {},
                  "note": "Snapshot of candidates only. No per-message publisher identity or subscriber receive times are captured."}
        for topic, _ in node.get_topic_names_and_types():
            pubs = [endpoint(e) for e in node.get_publishers_info_by_topic(topic)]
            subs = [endpoint(e) for e in node.get_subscriptions_info_by_topic(topic)]
            result["topics"][topic] = {"publishers": sorted({e["node"] for e in pubs}),
                                      "subscribers": sorted({e["node"] for e in subs}),
                                      "publisher_endpoints": pubs, "subscriber_endpoints": subs}
        Path(output).write_text(json.dumps(result, indent=2))
    finally:
        node.destroy_node()
        rclpy.shutdown()
