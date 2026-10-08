"""抢票工作线程：轮询 → 过滤 → 命中下单 → 排队 → 通知。"""
import logging
import random
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QThread, Signal

from .auth import Passenger
from .config import load_config
from .order import (AuthExpiredError, NoTicketError, OrderError,
                    QueueResult, submit_candidate, submit_order)
from .query import TicketFilter, TrainTicket, query_tickets
from .session import Session

log = logging.getLogger("ny12306.worker")


@dataclass
class GrabTask:
    """一个抢票任务的完整配置。"""
    task_id: str
    train_date: str            # YYYY-MM-DD
    from_code: str
    from_name: str
    to_code: str
    to_name: str
    filter: TicketFilter = field(default_factory=TicketFilter)
    passengers: list[Passenger] = field(default_factory=list)
    auto_candidate: bool = True    # 无票自动候补
    accept_no_seat: bool = False   # 无座兜底

    def title(self) -> str:
        return f"{self.from_name} → {self.to_name}  {self.train_date}"


class GrabWorker(QThread):
    sig_log = Signal(str)
    sig_tickets = Signal(list)        # list[TrainTicket]
    sig_status = Signal(str)          # 监控中/排队中/成功/失败/登录失效
    sig_success = Signal(str, str)    # order_id, 摘要

    def __init__(self, task: GrabTask, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.task = task
        self._session = session
        self._stop = False
        self.poll_count = 0

    def stop(self) -> None:
        self._stop = True

    # ---------- 主循环 ----------

    def run(self) -> None:
        cfg = load_config()
        interval = max(1.5, float(cfg.get("poll_interval", 2.0)))
        jitter = max(0.0, float(cfg.get("poll_jitter", 1.0)))
        backoff = 1.0
        self.sig_status.emit("监控中")
        self.log(f"任务启动：{self.task.title()}")
        while not self._stop:
            try:
                trains = query_tickets(
                    self._session, self.task.train_date,
                    self.task.from_code, self.task.to_code)
                self.poll_count += 1
                self.sig_tickets.emit(trains)
                backoff = 1.0
                if self._try_grab(trains):
                    return
            except AuthExpiredError:
                self.sig_status.emit("登录失效")
                self.log("登录状态已失效，请重新扫码登录")
                return
            except Exception as exc:
                self.log(f"轮询异常：{exc}（{backoff:.0f}s 后重试）")
                self._sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self._sleep(interval + random.uniform(0, jitter))
        self.sig_status.emit("已停止")

    # ---------- 抓取逻辑 ----------

    def _try_grab(self, trains: list[TrainTicket]) -> bool:
        """命中则下单，成功或用户停止返回 True（线程结束）。"""
        f = self.task.filter
        for t in trains:
            if self._stop:
                return True
            if not t.can_buy:
                continue
            if f.train_codes and t.code not in f.train_codes:
                continue
            if f.time_from and t.start_time < f.time_from:
                continue
            if f.time_to and t.start_time > f.time_to:
                continue
            seat = f.pick_seat(t)
            if seat is None and self.task.accept_no_seat and t.seat_available("无座"):
                seat = "无座"
            if seat is None:
                continue
            self.log(f"命中 {t.code} {seat}（{t.start_time} 发车），尝试下单")
            if self._do_order(t, seat):
                return True
        return False

    def _do_order(self, t: TrainTicket, seat: str) -> bool:
        self.sig_status.emit("下单中")
        try:
            outcome = submit_order(self._session, t, seat,
                                   self.task.passengers, self.log)
        except NoTicketError as exc:
            self.log(f"手慢了：{exc}，继续监控")
            self.sig_status.emit("监控中")
            if self.task.auto_candidate:
                return self._do_candidate(t, seat)
            return False
        self.sig_status.emit("排队中")
        if outcome.result is QueueResult.SUCCESS:
            summary = (f"{t.code} {t.date} {t.from_station}→{t.to_station} "
                       f"{seat} {t.start_time}开")
            self.sig_status.emit("已下单")
            self.sig_success.emit(outcome.order_id, summary)
            self.log(f"下单成功！订单号 {outcome.order_id}，请 10 分钟内到 12306 App 支付")
            return True
        self.log(f"排队失败：{outcome.message}")
        self.sig_status.emit("监控中")
        return False

    def _do_candidate(self, t: TrainTicket, seat: str) -> bool:
        try:
            outcome = submit_candidate(self._session, t, seat,
                                       self.task.passengers, self.log)
            if outcome.result is QueueResult.SUCCESS:
                self.sig_status.emit("候补中")
                self.log(f"候补订单已受理：{outcome.order_id}")
                return True
            self.log(f"候补提交失败：{outcome.message}")
        except OrderError as exc:
            self.log(f"候补提交异常：{exc}")
        return False

    # ---------- 工具 ----------

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        log.info("[%s] %s", self.task.task_id, msg)
        self.sig_log.emit(f"{ts} [{self.task.task_id}] {msg}")

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self._stop and time.time() < end:
            time.sleep(0.2)
