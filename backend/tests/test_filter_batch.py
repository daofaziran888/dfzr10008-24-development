"""数据筛选引擎批量执行的回归测试（仅依赖标准库）。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import unittest

from app.seed import SEED_ROWS
from app.services.filter import FilterService, ITEM_FAILED, ITEM_PENDING, ITEM_SUCCESS
from app.store import store


class FilterBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都从示例数据重新开始，避免前一个用例的批次/结果集互相干扰。
        store._tables = {name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()}
        store._registries = {}
        self.service = FilterService()

    def test_batch_marks_conflict_template_separately(self) -> None:
        outcome = self.service.execute_batch(
            ["FILT-0001", "FILT-0002", "FILT-0003"], target_source="订单表"
        )
        items = outcome["batch"]["结果项"]
        self.assertEqual([item["模板编号"] for item in items], ["FILT-0001", "FILT-0002", "FILT-0003"])
        self.assertEqual([item["状态"] for item in items], [ITEM_SUCCESS, ITEM_FAILED, ITEM_SUCCESS])
        failed = items[1]
        self.assertIn("日期字段", failed["说明"])
        self.assertIsNone(failed["结果集编号"])
        self.assertEqual(outcome["batch"]["汇总"], {"成功": 2, "失败": 1, "未执行": 0})
        # 失败的模板状态单独回落为已暂停并标记异常，成功的模板保持已启用。
        self.assertEqual(self.service.find_by_code("FILT-0002")["status"], "已暂停")
        self.assertTrue(self.service.find_by_code("FILT-0002")["abnormal"])
        self.assertEqual(self.service.find_by_code("FILT-0001")["status"], "已启用")

    def test_same_parameters_reuse_original_results(self) -> None:
        first = self.service.execute_batch(
            ["FILT-0001", "FILT-0002", "FILT-0003"], target_source="订单表"
        )
        twice = self.service.execute_batch(
            ["FILT-0001", "FILT-0002", "FILT-0003"], target_source="订单表"
        )
        self.assertEqual(first["batch"]["批次编号"], twice["batch"]["批次编号"])
        self.assertEqual(twice["batch"]["本次执行"], "复用")
        self.assertEqual(len(self.service.list_batches()[0]), 1)
        result_no = "BATCH-0001-FILT-0001"
        self.assertEqual(len(self.service.get_result(result_no)["行"]), 6)

    def test_downstream_entry_matches_result_set(self) -> None:
        self.service.execute_batch(["FILT-0001"], target_source="订单表")
        result = self.service.get_result("BATCH-0001-FILT-0001")
        analysis = self.service.analysis_entry("BATCH-0001-FILT-0001")
        self.assertEqual(analysis["展示顺序"], result["展示顺序"])
        self.assertEqual(analysis["筛选条件明细"], result["冻结条件"]["筛选条件明细"])
        self.assertEqual(analysis["筛选条件"], result["冻结条件"]["筛选条件"])
        self.assertEqual(analysis["行"], result["行"])

    def test_interrupted_batch_resumes_from_failed_template(self) -> None:
        started = self.service.execute_batch(
            ["FILT-0001", "FILT-0003"], target_source="订单表", stop_after="FILT-0001"
        )
        batch_no = started["batch"]["批次编号"]
        self.assertEqual(started["batch"]["汇总"], {"成功": 1, "失败": 0, "未执行": 1})
        self.assertEqual(started["batch"]["结果项"][1]["状态"], ITEM_PENDING)

        resumed = self.service.resume_batch(batch_no)
        self.assertEqual(resumed["batch"]["状态"], "已完成")
        self.assertEqual(resumed["batch"]["汇总"], {"成功": 2, "失败": 0, "未执行": 0})
        # 已成功的结果项没有重写，结果集编号保持一致。
        self.assertEqual(resumed["batch"]["结果项"][0]["结果集编号"], "BATCH-0001-FILT-0001")
        self.assertEqual(len(self.service.get_result("BATCH-0001-FILT-0001")["行"]), 6)

    def test_retry_does_not_rewrite_successful_result(self) -> None:
        self.service.execute_batch(["FILT-0001"], target_source="订单表")
        retried = self.service.retry_item("BATCH-0001", "FILT-0001")
        self.assertTrue(retried["ok"])
        self.assertIn("未重复写入", retried["message"])
        results = self.service.list_results()
        self.assertEqual(len(results), 1)

    def test_retry_failed_template_keeps_it_marked(self) -> None:
        self.service.execute_batch(["FILT-0002"], target_source="订单表")
        retried = self.service.retry_item("BATCH-0001", "FILT-0002")
        self.assertFalse(retried["ok"])
        self.assertEqual(retried["item"]["状态"], ITEM_FAILED)
        self.assertEqual(retried["batch"]["状态"], "部分失败")


if __name__ == "__main__":
    unittest.main()
