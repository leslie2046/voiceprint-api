import numpy as np
import torch
import time
import threading
from typing import Dict, List, Tuple
from modelscope.pipelines import pipeline
from modelscope.utils.constant import Tasks
from ..core.config import settings
from ..core.logger import get_logger
from ..database.voiceprint_db import voiceprint_db
from ..utils.audio_utils import audio_processor

logger = get_logger(__name__)


class VoiceprintService:
    """声纹识别服务类"""

    def __init__(self):
        self._pipeline = None
        self.similarity_threshold = settings.similarity_threshold
        self.model_id = settings.model_id
        self._pipeline_lock = threading.Lock()  # 添加线程锁
        self._init_pipeline()
        self._warmup_model()  # 添加模型预热

    def _init_pipeline(self) -> None:
        """初始化声纹识别模型"""
        start_time = time.time()
        logger.start("初始化声纹识别模型")

        try:
            # 检查CUDA可用性
            if torch.cuda.is_available():
                device = "gpu"
                logger.info(f"使用GPU设备: {torch.cuda.get_device_name(0)}")
            else:
                device = "cpu"
                logger.info("使用CPU设备")

            logger.info(f"开始加载模型: {self.model_id}")
            self._pipeline = pipeline(
                task=Tasks.speaker_verification,
                model=self.model_id,
                device=device,
            )

            init_time = time.time() - start_time
            logger.complete("初始化声纹识别模型", init_time)
        except Exception as e:
            init_time = time.time() - start_time
            logger.fail(f"声纹模型加载失败，耗时: {init_time:.3f}秒，错误: {e}")
            raise

    def _warmup_model(self) -> None:
        """模型预热，避免第一次推理的延迟"""
        start_time = time.time()
        logger.start("开始模型预热")

        try:
            # 预热librosa重采样组件
            logger.debug("预热librosa重采样组件...")
            import librosa
            import soundfile as sf
            import tempfile

            # 生成一个更真实的测试音频（1秒的随机音频，模拟真实语音）
            sample_rate = 16000
            duration = 1.0  # 1秒
            samples = int(sample_rate * duration)

            # 生成随机音频数据，模拟真实语音
            np.random.seed(42)  # 固定随机种子，确保可重现
            test_audio = (
                np.random.randn(samples).astype(np.float32) * 0.1
            )  # 小幅度随机音频

            # 创建不同采样率的音频文件进行预热
            test_rates = [8000, 22050, 44100, 16000]  # 测试不同采样率

            for test_rate in test_rates:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmpf:
                    # 生成测试采样率的音频
                    test_samples = int(test_rate * duration)
                    test_audio_resampled = librosa.resample(
                        test_audio, orig_sr=sample_rate, target_sr=test_rate
                    )
                    sf.write(tmpf.name, test_audio_resampled, test_rate)
                    temp_audio_path = tmpf.name

                try:
                    # 使用音频处理器处理这个文件（预热音频处理流程）
                    with open(temp_audio_path, "rb") as f:
                        audio_bytes = f.read()

                    # 预热音频处理
                    processed_path = audio_processor.ensure_16k_wav(audio_bytes)

                    # 预热模型推理
                    with self._pipeline_lock:
                        result = self._pipeline([processed_path], output_emb=True)
                        emb = self._to_numpy(result["embs"][0]).astype(np.float32)
                        logger.debug(
                            f"模型预热完成 ({test_rate}Hz -> 16kHz)，特征维度: {emb.shape}"
                        )

                    # 清理处理后的文件
                    audio_processor.cleanup_temp_file(processed_path)

                finally:
                    # 清理临时文件
                    import os

                    if os.path.exists(temp_audio_path):
                        os.unlink(temp_audio_path)

            warmup_time = time.time() - start_time
            logger.complete("模型预热完成", warmup_time)

        except Exception as e:
            warmup_time = time.time() - start_time
            logger.warning(f"模型预热失败，耗时: {warmup_time:.3f}秒，错误: {e}")
            # 预热失败不影响服务启动，只记录警告

    def _to_numpy(self, x) -> np.ndarray:
        """
        将torch tensor或其他类型转为numpy数组

        Args:
            x: 输入数据

        Returns:
            np.ndarray: numpy数组
        """
        return x.cpu().numpy() if torch.is_tensor(x) else np.asarray(x)

    def extract_voiceprint(self, audio_path: str) -> np.ndarray:
        """
        从音频文件中提取声纹特征

        Args:
            audio_path: 音频文件路径

        Returns:
            np.ndarray: 声纹特征向量
        """
        start_time = time.time()
        logger.start(f"提取声纹特征，音频文件: {audio_path}")

        try:
            # 使用线程锁确保模型推理的线程安全
            with self._pipeline_lock:
                pipeline_start = time.time()
                logger.debug("开始模型推理...")

                # 检查pipeline是否可用
                if self._pipeline is None:
                    raise RuntimeError("声纹模型未初始化")

                result = self._pipeline([audio_path], output_emb=True)
                pipeline_time = time.time() - pipeline_start
                logger.debug(f"模型推理完成，耗时: {pipeline_time:.3f}秒")

            convert_start = time.time()
            emb = self._to_numpy(result["embs"][0]).astype(np.float32)
            convert_time = time.time() - convert_start
            logger.debug(f"数据转换完成，耗时: {convert_time:.3f}秒")

            total_time = time.time() - start_time
            logger.complete(f"提取声纹特征，维度: {emb.shape}", total_time)
            return emb
        except Exception as e:
            total_time = time.time() - start_time
            logger.fail(f"声纹特征提取失败，总耗时: {total_time:.3f}秒，错误: {e}")
            raise

    def calculate_similarity(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        """
        计算两个声纹特征的相似度

        Args:
            emb1: 声纹特征1
            emb2: 声纹特征2

        Returns:
            float: 相似度分数 (0-1)
        """
        try:
            # 使用余弦相似度
            similarity = np.dot(emb1, emb2) / (
                np.linalg.norm(emb1) * np.linalg.norm(emb2)
            )
            return float(similarity)
        except Exception as e:
            logger.error(f"相似度计算失败: {e}")
            return 0.0

    def register_voiceprint(self, speaker_id: str, audio_bytes: bytes) -> bool:
        """
        注册声纹

        Args:
            speaker_id: 说话人ID
            audio_bytes: 音频字节数据

        Returns:
            bool: 注册是否成功
        """
        audio_path = None
        try:
            # 简化音频验证，只做基本检查
            if len(audio_bytes) < 1000:  # 文件太小
                logger.warning(f"音频文件过小: {speaker_id}")
                return False

            # 处理音频文件
            audio_path = audio_processor.ensure_16k_wav(audio_bytes)

            # 提取声纹特征
            emb = self.extract_voiceprint(audio_path)

            # 保存到数据库
            success = voiceprint_db.save_voiceprint(speaker_id, emb)

            if success:
                logger.info(f"声纹注册成功: {speaker_id}")
            else:
                logger.error(f"声纹注册失败: {speaker_id}")

            return success

        except Exception as e:
            logger.error(f"声纹注册异常 {speaker_id}: {e}")
            return False
        finally:
            # 清理临时文件
            if audio_path:
                audio_processor.cleanup_temp_file(audio_path)

    def identify_voiceprint(
        self, speaker_ids: List[str], audio_bytes: bytes
    ) -> Tuple[str, float]:
        """
        识别声纹

        Args:
            speaker_ids: 候选说话人ID列表
            audio_bytes: 音频字节数据

        Returns:
            Tuple[str, float]: (识别出的说话人ID, 相似度分数)
        """
        start_time = time.time()
        logger.info(f"开始声纹识别流程，候选说话人数量: {len(speaker_ids)}")

        audio_path = None
        try:
            # 简化音频验证
            if len(audio_bytes) < 1000:
                logger.warning("音频文件过小")
                return "", 0.0

            # 处理音频文件
            audio_process_start = time.time()
            audio_path = audio_processor.ensure_16k_wav(audio_bytes)
            audio_process_time = time.time() - audio_process_start
            logger.debug(f"音频文件处理完成，耗时: {audio_process_time:.3f}秒")

            # 提取声纹特征
            extract_start = time.time()
            logger.debug("开始提取声纹特征...")
            test_emb = self.extract_voiceprint(audio_path)
            extract_time = time.time() - extract_start
            logger.debug(f"声纹特征提取完成，耗时: {extract_time:.3f}秒")

            # 获取候选声纹特征
            db_query_start = time.time()
            logger.debug("开始查询数据库获取候选声纹特征...")
            voiceprints = voiceprint_db.get_voiceprints(speaker_ids)
            db_query_time = time.time() - db_query_start
            logger.debug(
                f"数据库查询完成，获取到{len(voiceprints)}个声纹特征，耗时: {db_query_time:.3f}秒"
            )

            if not voiceprints:
                logger.info("未找到候选说话人声纹")
                return "", 0.0

            # 计算相似度
            similarity_start = time.time()
            logger.debug("开始计算相似度...")
            similarities = {}
            for name, emb in voiceprints.items():
                similarity = self.calculate_similarity(test_emb, emb)
                similarities[name] = similarity
            similarity_time = time.time() - similarity_start
            logger.debug(
                f"相似度计算完成，共计算{len(similarities)}个，耗时: {similarity_time:.3f}秒"
            )

            # 找到最佳匹配
            if not similarities:
                return "", 0.0

            match_name = max(similarities, key=similarities.get)
            match_score = similarities[match_name]

            # 检查是否超过阈值
            if match_score < self.similarity_threshold:
                logger.info(
                    f"未识别到说话人，最高分: {match_score:.4f}，阈值: {self.similarity_threshold}"
                )
                total_time = time.time() - start_time
                logger.info(f"声纹识别流程完成，总耗时: {total_time:.3f}秒")
                return "", match_score

            total_time = time.time() - start_time
            logger.info(
                f"识别到说话人: {match_name}, 分数: {match_score:.4f}, 总耗时: {total_time:.3f}秒"
            )
            return match_name, match_score

        except Exception as e:
            total_time = time.time() - start_time
            logger.error(f"声纹识别异常，总耗时: {total_time:.3f}秒，错误: {e}")
            return "", 0.0
        finally:
            # 清理临时文件
            cleanup_start = time.time()
            if audio_path:
                audio_processor.cleanup_temp_file(audio_path)
            cleanup_time = time.time() - cleanup_start
            logger.debug(f"临时文件清理完成，耗时: {cleanup_time:.3f}秒")

    def delete_voiceprint(self, speaker_id: str) -> bool:
        """
        删除声纹

        Args:
            speaker_id: 说话人ID

        Returns:
            bool: 删除是否成功
        """
        return voiceprint_db.delete_voiceprint(speaker_id)

    def get_voiceprint_count(self) -> int:
        """
        获取声纹总数

        Returns:
            int: 声纹总数
        """
        start_time = time.time()
        logger.info("开始获取声纹总数...")

        try:
            count = voiceprint_db.count_voiceprints()
            total_time = time.time() - start_time
            logger.info(f"声纹总数获取完成: {count}，耗时: {total_time:.3f}秒")
            return count
        except Exception as e:
            total_time = time.time() - start_time
            logger.error(f"获取声纹总数失败，总耗时: {total_time:.3f}秒，错误: {e}")
            raise


# 全局声纹服务实例
voiceprint_service = VoiceprintService()
