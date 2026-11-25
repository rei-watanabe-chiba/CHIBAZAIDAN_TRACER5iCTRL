"""アプリを閉じるときの照射の止め方(画面を持たない)"""
from __future__ import annotations

import time

import auto_measure as am
import device_link as dl


def release_trigger(pump=lambda: time.sleep(0.1), log=lambda msg: None, tries: int = 3, wait_s: float = 2.0) -> bool:
    """照射中(トリガーが引かれたまま)なら離して、止まったことを確かめる。止まった・照射していなければ True。
    自動測定中でなくても(機器の画面を手で操作して照射していても)止める。pump: 待つ間に呼ぶ(Tk は画面を動かす)"""
    if dl.current_link() is None:
        return True                                   # 機器につながっていない = このアプリから照射はできていない
    try:
        op = am.Operator()
    except Exception:
        return True
    for _ in range(tries):
        if op.trigger_state() != "release":           # pull(待機)/ unknown(つながっていない)
            return True
        am.stop_irradiation(op)                               # = Trigger Release(止めるだけ。照射を始める方へは切り替えない)
        t = time.time()
        while time.time() - t < wait_s:
            pump()
            if op.trigger_state() != "release":
                log("終了のため照射を止めました(トリガー解除)")
                return True
    return False
