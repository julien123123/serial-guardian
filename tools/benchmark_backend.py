"""Compare old and new session-log lookup on a temporary 5,000-session fixture.

Run with Guardian's Python environment: python tools/benchmark_backend.py
The temporary fixture is removed after the benchmark.
"""
import copy, glob, json, os, statistics, sys, tempfile, time, types
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from monitor import SerialSessionMonitor
from webapp import create_app
with tempfile.TemporaryDirectory(prefix='guardian-bench-') as temp:
    cfg=types.SimpleNamespace(**{key:copy.deepcopy(getattr(config,key)) for key in dir(config) if key.isupper()})
    cfg.DATA_DIR=temp
    logs=Path(temp)/'sessions';logs.mkdir()
    records=[]
    for sid in range(1,5001):
        status=['NORMAL','ANOMALY','EXCEPTION','STOPPED'][sid%4]
        filename=f'{sid:06d}_{status}.log'
        records.append(dict(id=sid,status=status,start=1000+sid,duration=.1,lines=4,file=filename))
        (logs/filename).write_text('REFRESH: 12\nreset cause = 0\nwake up pins []\n'+'log data\n'*40)
    Path(temp,'index.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    monitor=SerialSessionMonitor(cfg)
    direct=monitor.read_session_text
    def old_read(sid,max_chars=None):
        matches=glob.glob(os.path.join(monitor.sessions_dir,f'{sid:06d}_*.log'))
        if not matches:return None
        with open(matches[0]) as f:return f.read(max_chars) if max_chars is not None else f.read()
    client=create_app(monitor,cfg).test_client()
    output={}
    for name,method in [('directory_scan',old_read),('direct_paths',direct)]:
        monitor.read_session_text=method
        client.get('/sessions')
        times=[];cpu=[]
        for _ in range(5):
            wall=time.perf_counter();proc=time.process_time()
            result=client.get('/sessions');assert result.status_code==200
            cpu.append((time.process_time()-proc)*1000);times.append((time.perf_counter()-wall)*1000)
        output[name]=dict(median_wall_ms=round(statistics.median(times),2),median_cpu_ms=round(statistics.median(cpu),2))
    print(json.dumps(output))
    print('speedup',round(output['directory_scan']['median_wall_ms']/output['direct_paths']['median_wall_ms'],1))
