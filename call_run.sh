set -e
# ../build/debug/zsim my-config.cfg | tee stdout
# ../build/debug/zsim simple.cfg | tee stdout
python3 main.py trace/mcf_0.txt
curl https://sc.ftqq.com/SCU89394T728d08582f5c5f1537c4d3da3a772b9f5e6c8f62e7de3.send?text=Finish&desp=FinishTraceHMSmartSwap
