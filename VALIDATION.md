# Validation of version 0.1.0

Run in the development environment with Python 3.12.14, mcap 1.5.0,
rosbags 0.11.8, zstandard 0.25.0 and lz4 4.4.5.

- 52 automated tests passed. The tests cover serializer-independent fixtures,
  big/little endian CDR, custom MSG and IDL bundles, nested stamps, primitive and
  variable-size arrays, malformed timestamps, missing/ambiguous/disabled stamps,
  a 24 MiB primitive array before a stamp, MCAP compression and index variants,
  CRC rejection, lazy topic/time reads, persistent cache reuse, split-file
  ordering, exact statistics, histogram predicates and CLI output.
- A Chromium browser integration test passed: initial Canvas rendering, exact
  histogram-bin highlighting, switching to node mode, changing the record-time
  window and fuzzy topic selection. No JavaScript errors were observed. Topic
  and node views were visually inspected. The optional test script is
  `tests/browser_smoke.py`.
- The statistics CLI produced JSON, CSV and six-panel PNG files. Sample PNG
  layout was visually inspected. Examples are in `examples/statistics`; they use
  the first second of the included 3,000-message bag.
- One million synthetic small messages were indexed and analyzed. Density counts
  and statistics counts both equaled 1,000,000. See `examples/benchmark.json` for
  measured timing and memory; `tests/benchmark.py` reproduces the procedure.
- Python wheel and source-distribution builds succeeded. Web assets are included
  in the wheel. The installable wheel is included under `dist` in the ZIP.

Not tested here: an actual user/hardware bag, live ROS graph capture, the optional
ament wrapper in a ROS installation, non-Linux platforms, or a broad collection
of vendor-specific IDL and CDR encodings. Unsupported stamp layouts retain
recording/publication times and report a stamp extraction status. This release
does not reconstruct real subscriber receive events or import traces.
