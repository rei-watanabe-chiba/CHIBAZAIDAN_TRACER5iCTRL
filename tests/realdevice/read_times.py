# 各モードの設定画面の時間欄を読むだけ(書き換えずキャンセルで閉じる)
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Traer5iAuto", "src"))
import auto_measure as am, device_link as dl
op = am.Operator()
for app in sys.argv[1:]:
    am.switch_app(op, app)
    am.to_main(op); ui = op.ui or op.read()
    b = ui.find_button("設定", 0.6)
    op.click_elem(ui, b, wait=1.0)
    ui = op.wait_state("settings", 10)
    f = am.time_fields(ui)
    vals = [am.field_value(ui, x, None) for x in f]
    print(f"{app}: 欄 {len(f)} 個 {vals}  デフォルト設定を使用={'オン' if not f else 'オフ'}")
    am._close_settings(op, save=False)
am.to_main(op)
dl.close_link()
