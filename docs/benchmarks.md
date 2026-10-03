# Benchmark environment

The timings in this documentation, in the changelog and in the blog example
were measured on one machine. They show how the options compare with DRF on
that machine; they are not a guarantee for another one. Measure your own
serializers and payloads on your own hardware before choosing an option.

## Reference machine

| | |
| --- | --- |
| CPU | Intel Core i7-14700F: 8 performance cores (16 threads, up to 5.3–5.4 GHz) and 12 efficiency cores (up to 4.2 GHz), 33 MiB L3 cache |
| Memory | 128 GB |
| System | Ubuntu 26.04.1 LTS, Linux 7.0, bare metal |
| CPU settings | `powersave` frequency governor with turbo enabled, simultaneous multithreading on |
| Python | CPython 3.14.4 (GCC 15.2), with the GIL |
| Packages | Django 6.1.1, DRF 3.18.1, msgspec 0.22.0, Pydantic 2.13.5, orjson 3.12.0 |

The [JSON transport measurements](json-transport-performance.md) also give
results for the minimum supported versions (CPython 3.12.14, Django 5.2,
DRF 3.16.0) on the same machine.

## Method

- Microbenchmarks run in one process pinned to a performance core
  (`taskset -c 4`), after a warm-up, and report the best or the median of
  seven repeats.
- Other services (database containers) were running on the machine: expect
  a few percent of noise between runs.
- Before and after timings of one change are taken in alternating runs of
  the two versions, on the same core.

## What changes the results

- The processor: its microarchitecture, clock speed and turbo behaviour,
  cache sizes, and whether the process runs on a performance or an
  efficiency core of a hybrid CPU.
- The Python build and version: free-threaded CPython is slower for this
  code, and each CPython release changes the interpreter's speed.
- The versions of Django, DRF and the serialization libraries.
- The workload: the number of rows and fields, the field types, the
  payload's strings and nesting, and the database queries around it.
- Concurrency, which these single-threaded measurements do not include.

Ratios between DRF and an option carry over to similar hardware better than
absolute times, but they too depend on the points above.
