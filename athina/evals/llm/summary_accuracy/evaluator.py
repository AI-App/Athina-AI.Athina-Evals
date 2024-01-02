from enum import Enum
import pprint
import time
import traceback
from typing import List, Optional
from athina.interfaces.model import Model
from athina.interfaces.result import LlmEvalResult, LlmEvalResultMetric, BatchRunResult
from athina.loaders.summary_loader import SummaryDataPoint
from athina.metrics.metric_type import MetricType
from ..llm_evaluator import LlmEvaluator
from ..eval_type import AthinaEvalTypeId
from ..example import FewShotExample
from athina.llms.question_answerer import QuestionAnswerer
from athina.llms.question_answerer_bulk import QuestionAnswererBulk
from athina.llms.question_answerer_cot import QuestionAnswererChainOfThought
from athina.llms.question_generator import QuestionGenerator

class SummaryAccuracy(LlmEvaluator):
    """
    This evaluator can be configured with custom examples and instructions.
    """
    questions: List[str] = []

    def __init__(
        self,
        questions: Optional[List[str]] = None,
        n_questions: int = 10,
        model: str = "gpt-4-1106-preview",
        question_answerer: Optional[QuestionAnswerer] = None,
    ):
        """
        Initialize the evaluator with given parameters.

        Args:
        - loader: An instance of SummarizationLoader.
        - n_questions: Number of questions to generate for summaries.
        - llm_model: Language model to be used.
        - metrics: List of metrics for evaluation.
        """

        # Intialize LLMs
        self._model = model
        self.n_questions = n_questions
        if questions is not None:
            self.questions = questions
        self.question_generator = QuestionGenerator(
            self._model, n_questions
        )
        if question_answerer is None:
            self.question_answerer = QuestionAnswererBulk(model=self._model)
        else:
            self.question_answerer = question_answerer
        self.n_instances = 0
        self.label_counts = {}
        for metric in self.metric_ids:
            setattr(self, f"{metric}_scores", {})

    @property
    def name(self):
        return AthinaEvalTypeId.SUMMARY_ACCURACY.value
        
    @property
    def metric_ids(self) -> str:
        return [
            MetricType.AGREEMENT_SCORE,
            MetricType.CONTRADICTION_SCORE,
            MetricType.HALLUCINATION_SCORE
        ]
        
    @property
    def display_name(self):
        return "Summary Accuracy"
        
    @property
    def default_model(self):
        return Model.GPT35_TURBO.value
        
    @property
    def required_args(self):
        return ["document", "response"]
        
    @property
    def examples(self):
        return []
        
    def is_failure(self) -> bool:
        return False
    
    def reason(self) -> str:
        disagreement_answers = self._disagreement_answers()
        if len(disagreement_answers) == 0:
            return "No disagreement between document and summary."
        reason_str = ""
        for question, answer_doc, answer_sum in disagreement_answers:
            reason_str += f"{question}\n- Document: {answer_doc}\n- Summary: {answer_sum}\n"
        return reason_str


    def _evaluate(self, **instance) -> LlmEvalResult:
        """
        Run the LLM evaluator.
        """
        start_time = time.time()

        # Validate that correct args were passed
        self._validate_args(**instance)

        summary_datapoint = SummaryDataPoint(**instance)

        # Run the Summary Accuracy evaluator
        summary_eval_result = self._evaluate_element(summary_datapoint)

        end_time = time.time()
        eval_runtime_ms = int((end_time - start_time) * 1000)
        
        metrics = [{ "id": metric_id.value, "value": summary_eval_result[metric_id.value] } for metric_id in self.metric_ids]

        llm_eval_result = LlmEvalResult(
            name=self.name,
            display_name=self.display_name,
            data=SummaryDataPoint(**instance),
            failure=self.is_failure(),
            reason=self.reason(),
            runtime=eval_runtime_ms,
            model=self._model,
            metrics=metrics,
        )

        return {k: v for k, v in llm_eval_result.items() if v is not None}
    
    def _disagreement_answers(self):
        """Return the questions for which the Y/N answers do not match between document and summary."""
        disagreement_answers = []
        for question in self.answers_doc:
            answer_doc = self.answers_doc[question]
            answer_sum = self.answers_sum[question]
            if answer_doc != answer_sum:
                disagreement_answers.append((question, answer_doc, answer_sum))
        return disagreement_answers

    def _evaluate_element(self, instance: SummaryDataPoint):
        """Evaluate an instance for hallucination."""
        try:
            # Parse instance
            document = instance["document"]
            summary = instance["response"]
            if "label" in instance:
                label = instance["label"]
            else:
                label = "overall"
        except Exception as e:
            print("Exception while parsing instance", e)
            traceback.print_exc()
            raise e

        try:
            # Generate questions based on summary
            if self.questions is None or len(self.questions) == 0:
                self.questions = self.question_generator.generate(summary)

            self.answers_doc = self.question_answerer.answer(questions=self.questions, context=document)[1]
            self.answers_sum = self.question_answerer.answer(questions=self.questions, context=summary)[1]
            metric_results = {}
        except Exception as e:
            print("Exception while generating answers", e)
            traceback.print_exc()
            raise e

        try:
            # Compute metrics
            if self.answers_doc is None or self.answers_sum is None or self.questions is None:
                raise Exception("Validation error - unable to generate answers")
            else:
                for metric in self.metric_ids:
                    metric_name = metric.value
                    metric_class = metric.get_class()
                    metric_result, explanation = metric_class.compute(
                        self.answers_doc, self.answers_sum, self.questions, self.n_questions
                    )
                    metric_results[metric_name] = metric_result
                    metric_results[f"reason_{metric_name}"] = explanation
                    self.update_metric_aggregated_score(metric_name, label, metric_result)
                self.n_instances = self.n_instances + 1
                self.label_counts[label] = self.label_counts.get(label, 0) + 1
            return {
                "questions": self.questions,
                "answers_doc": self.answers_doc,
                "answers_sum": self.answers_sum,
                "label": label,
                **metric_results,
            }
        except Exception as e:
            print("Exception while computing metrics", e)
            traceback.print_exc()
            raise e

    def update_metric_aggregated_score(self, metric, label, aggr_score):
        """Update the aggregated score for a specific metric and label."""
        metric_aggregated_scores = getattr(self, f"{metric}_scores", {})
        current_score = metric_aggregated_scores.get(label, 0)
        metric_aggregated_scores[label] = current_score + aggr_score
        setattr(self, f"{metric}_scores", metric_aggregated_scores)

    def get_metric_aggr(self, metric, label):
        """Compute the average scores based on the provided score dictionary."""
        metric_aggr = getattr(self, f"{metric}_scores", {})
        return metric_aggr.get(label, None)

    def get_average_scores(self, score_dict):
        """Compute average scores for a metric"""
        avg_scores = {}
        sum_score = 0
        n_instances = 0
        for label_type, total_score in score_dict.items():
            avg_scores[label_type] = total_score / self.label_counts[label_type]
            sum_score = sum_score + total_score
            n_instances = n_instances + self.label_counts[label_type]
        avg_scores["overall"] = sum_score / n_instances
        return avg_scores

    def compute_average_scores(self):
        """Compute average scores for each metric."""
        avg_scores = {}
        for metric in self.metric_ids:
            scores = getattr(self, f"{metric}_scores")
            avg_score = self.get_average_scores(scores)
            avg_scores[metric] = avg_score
        return avg_scores

    