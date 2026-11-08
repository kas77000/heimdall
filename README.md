# Heimdall

Heimdall watches for conditions across KDB, Bloomberg and in-house scripts,
and raises an alert when one is met. It is the alerting half of what
KdbMonitor used to do, rebuilt as a standing service so that checks run
whether or not anyone has a browser open. Dashboards stay in KdbMonitor.

## Parts

| Part | Role |
|---|---|
| **Huginn** | The service that goes out and checks KDB, Bloomberg and your scripts, evaluates each alert's chain of steps, and sends notifications. |
| **Muninn** | The store: alert definitions, the history of every run and every firing, and cleanup of old history. |
| **Sleipnir** | The scheduler that turns alerts on and off on time. |
| **Bifrost** | The Streamlit app where alerts are built and their history is read. |

In the myth, Heimdall keeps watch over Bifrost, Huginn ("thought") and
Muninn ("memory") are the ravens that fly out each day and report back, and
Sleipnir is Odin's eight-legged horse.

## Status

Design in progress.
