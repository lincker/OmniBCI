"""A killed Windows sidecar cannot leave a device subprocess behind."""
import os
from pathlib import Path
import subprocess
import sys
from queue import Queue
from threading import Thread

import pytest


@pytest.mark.integration
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are platform-specific")
def test_killing_job_owner_also_terminates_its_child():
    import ctypes
    from ctypes import wintypes

    script = """
import subprocess, sys
from omnibci.infrastructure.process_tree import own_windows_process_tree
job = own_windows_process_tree()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
print(child.pid, flush=True)
sys.stdin.readline()
"""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    process = subprocess.Popen(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    child_handle = None
    try:
        ready = Queue()
        Thread(target=lambda: ready.put(process.stdout.readline().strip()), daemon=True).start()
        child_pid = ready.get(timeout=15)
        assert child_pid, "Job owner exited before starting its child"
        child_handle = kernel.OpenProcess(0x00100001, False, int(child_pid))
        assert child_handle
        process.kill()
        process.wait(timeout=10)
        assert kernel.WaitForSingleObject(child_handle, 10000) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if child_handle:
            kernel.TerminateProcess(child_handle, 1)
            kernel.CloseHandle(child_handle)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
