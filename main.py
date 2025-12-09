from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import aiohttp
import asyncio
import json
import re
import time
from datetime import datetime
import base64
import io  # <--- 新增
# 尝试导入图片处理库
try:
    from PIL import Image as PyImage
except ImportError:
    PyImage = None
@register("gemini-draw", "Flow2API", "谷歌绘图插件 (纯base64版)", "8.6")
class GeminiDraw(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.api_url = config.get("api_url", "http://172.17.0.1:8000/v1/chat/completions")
        self.apikey = config.get("apikey", "")

        # 定义所有可用模型（新增完整列表）
        self.available_models = [
            "gemini-2.5-flash-image-landscape",  # Gemini 2.5 Flash 横屏
            "gemini-2.5-flash-image-portrait",  # Gemini 2.5 Flash 竖屏
            "gemini-3.0-pro-image-landscape",  # Gemini 3.0 Pro 横屏
            "gemini-3.0-pro-image-portrait",  # Gemini 3.0 Pro 竖屏
            "imagen-4.0-generate-preview-landscape",  # Imagen 4.0 横屏
            "imagen-4.0-generate-preview-portrait",  # Imagen 4.0 竖屏
        ]

        # 设置默认模型（可配置）
        default_model = config.get("model", "imagen-4.0-generate-preview-landscape")
        if default_model in self.available_models:
            self.current_model = default_model
        else:
            self.current_model = self.available_models[0]  # 回退到第一个

        # 第三方转换API配置
        self.convert_api_url = config.get("convert_api_url", "https://api.s01s.cn/API/url_ba64/")
        self.enable_convert_api = config.get("enable_convert_api", True)

        self.prompt_map: dict = {}
        self._load_prompt_map(config)
        logger.info(f"GeminiDraw 初始化完成，当前模型: {self.current_model}")
        logger.info(f"可用模型数: {len(self.available_models)}")
        logger.info(f"转换API状态: {'启用' if self.enable_convert_api else '禁用'}")

    def _load_prompt_map(self, config: dict):
        """加载自定义提示词映射"""
        self.prompt_map.clear()
        prompt_list = config.get("prompt_list", [])
        if not prompt_list:
            return
        for item in prompt_list:
            try:
                if ":" in item:
                    key, value = item.split(":", 1)
                    self.prompt_map[key.strip()] = value.strip()
            except ValueError:
                continue
        logger.info(f"已加载 {len(self.prompt_map)} 个自定义提示词")

        # ---------------- 新增：智能图片处理逻辑 ----------------
        # ---------------- 核心：智能图片处理 (GIF裁切+压缩) ----------------
    async def _process_image_url(self, img_url: str) -> str:
        """
        逻辑：
        1. 下载图片二进制数据
        2. 使用 Pillow 读取
        3. 如果是 GIF -> 取第一帧 -> 转 RGB -> 压缩 -> 转 Base64
        4. 如果是普通图片 -> 同样压缩以提高成功率
        """
        if img_url.startswith("data:image/"):
            return img_url

        # 如果没有安装 Pillow，回退到旧逻辑（防止报错）
        if PyImage is None:
            logger.warning("❌ 未安装 Pillow 库，无法裁切 GIF，正在使用原图模式")
            return await self._convert_url_to_base64_via_api(img_url)

        logger.info(f"⬇️ 正在下载并裁切图片: {img_url[:50]}...")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(img_url, timeout=30) as resp:
                    if resp.status != 200:
                        return f"下载失败: {resp.status}"

                    img_data = await resp.read()

                    # === 使用 Pillow 处理图片 (核心修改) ===
                    try:
                        # 1. 读取图片
                        img = PyImage.open(io.BytesIO(img_data))

                        # 2. 如果是动图，seek到第一帧
                        img.seek(0)

                        # 3. 转换为 RGB (去除透明通道/GIF索引颜色，防止JPG保存失败)
                        img = img.convert("RGB")

                        # 4. 尺寸限制 (防止图片过大导致API超时，限制最大边长1536)
                        max_size = 1536
                        if img.width > max_size or img.height > max_size:
                            img.thumbnail((max_size, max_size))
                            logger.info(f"📉 图片尺寸已缩放至: {img.size}")

                        # 5. 保存为 JPG 并输出 Base64
                        buffer = io.BytesIO()
                        img.save(buffer, format="JPEG", quality=85)  # 85质量通常足够且体积小
                        b64_data = base64.b64encode(buffer.getvalue()).decode('utf-8')

                        # 强制返回 jpeg 头部，模型最容易识别
                        final_data = f"data:image/jpeg;base64,{b64_data}"

                        logger.info(f"✅ 图片处理成功: 原大小{len(img_data)} -> 新Base64长{len(b64_data)}")
                        return final_data

                    except Exception as pil_err:
                        logger.error(f"❌ Pillow 处理失败: {pil_err}")
                        # 如果 Pillow 处理失败，回退到 API
                        return await self._convert_url_to_base64_via_api(img_url)

        except Exception as e:
            logger.error(f"❌ 图片下载流程异常: {e}")
            return f"处理异常: {str(e)}"
    # ---------------- 核心：第三方API转换函数 ----------------
    async def _convert_url_to_base64_via_api(self, img_url: str) -> str:
        """调用第三方API将URL转换为Base64 - 简化版本"""
        if not self.enable_convert_api:
            logger.error("❌ 转换API已禁用，无法处理外部图片")
            return ""

        logger.info(f"🔄 调用第三方API转换图片URL: {img_url[:100]}...")

        try:
            params = {"url": img_url}

            async with aiohttp.ClientSession() as session:
                timeout = aiohttp.ClientTimeout(total=30)
                async with session.get(self.convert_api_url, params=params, timeout=timeout) as response:
                    response_text = await response.text()
                    logger.info(f"🔍 API返回状态: {response.status}")
                    logger.info(f"🔍 API返回预览: {response_text[:200]}...")

                    if response.status == 200:
                        full_content = response_text.strip()

                        # 方法1：使用正则表达式提取base64（最可靠）
                        import re
                        base64_match = re.search(r'"base64"\s*:\s*"([^"]+)"', full_content)
                        if base64_match:
                            b64_data = base64_match.group(1)
                            logger.info(f"✅ 正则提取base64成功，长度: {len(b64_data)}")

                            # 清理base64数据
                            b64_clean = b64_data.replace("data:image/jpeg;base64,", "") \
                                .replace("data:image/png;base64,", "") \
                                .replace("data:image/webp;base64,", "") \
                                .replace("data:image/gif;base64,", "")

                            if len(b64_clean) > 100:
                                logger.info(f"✅ 转换成功！Base64长度: {len(b64_clean)}")
                                return f"data:image/jpeg;base64,{b64_clean}"
                            else:
                                logger.warning(f"❌ 获取的Base64太短: {len(b64_clean)}")
                                return f"data:image/jpeg;base64,{b64_clean}"

                        # 方法2：尝试JSON解析
                        try:
                            json_data = json.loads(full_content)
                            if isinstance(json_data, dict):
                                if "base64" in json_data and json_data["base64"]:
                                    b64_data = json_data["base64"]
                                    logger.info(f"✅ JSON提取base64成功，长度: {len(b64_data)}")
                                    return f"data:image/jpeg;base64,{b64_data}"
                                elif "data" in json_data and json_data["data"]:
                                    b64_data = json_data["data"]
                                    logger.info(f"✅ JSON提取data字段成功，长度: {len(b64_data)}")
                                    return f"data:image/jpeg;base64,{b64_data}"
                        except json.JSONDecodeError:
                            logger.warning("❌ JSON解析失败，但已通过正则提取")

                        # 如果以上方法都失败，返回调试信息
                        debug_info = f"""
============== 转换API返回数据 (调试用) ==============
URL: {img_url}
状态码: {response.status}
API地址: {self.convert_api_url}

❌ 未能提取base64数据

原始返回内容:
{full_content[:1000]}...
"""
                        return debug_info
                    else:
                        logger.error(f"❌ 转换API请求失败: {response.status}")
                        error_text = await response.text()
                        debug_info = f"""
============== 转换API请求失败 ==============
URL: {img_url}
状态码: {response.status}
API地址: {self.convert_api_url}

错误响应:
{error_text[:500]}...
"""
                        return debug_info

        except asyncio.TimeoutError:
            logger.error("❌ 转换API请求超时")
            debug_info = f"""
============== 转换API请求超时 ==============
URL: {img_url}
API地址: {self.convert_api_url}
超时时间: 30秒

⚠️ 请求超时，请检查网络连接或API服务状态
"""
            return debug_info
        except Exception as e:
            logger.error(f"❌ 转换过程异常: {str(e)}")
            debug_info = f"""
============== 转换过程异常 ==============
URL: {img_url}
API地址: {self.convert_api_url}

异常信息: {str(e)}
"""
            return debug_info

    # ---------------- 核心：只提取图片URL ----------------
    async def _extract_image_url_from_event(self, event: AstrMessageEvent) -> str:
        """从消息事件中提取图片URL"""
        chain = event.message_obj.message
        logger.info("=== 开始提取图片URL ===")

        # 1. 检查Image对象的url属性
        for seg in chain:
            if isinstance(seg, Image):
                logger.info("找到 Image 对象")

                # 优先检查url属性（这是主要目标）
                if hasattr(seg, 'url') and seg.url:
                    url = seg.url
                    logger.info(f"✅ 找到图片URL: {url[:100]}...")
                    return url

                # 如果有base64，这是特殊情况（可能是本地生成的）
                if hasattr(seg, 'base64') and seg.base64:
                    b64_data = seg.base64
                    logger.info(f"⚠️ 找到base64数据，直接使用，长度: {len(b64_data)}")
                    return f"data:image/jpeg;base64,{b64_data}"

        # 2. 检查引用回复中的图片
        for seg in chain:
            if isinstance(seg, Reply):
                if hasattr(seg, 'chain'):
                    for reply_seg in seg.chain:
                        if isinstance(reply_seg, Image):
                            logger.info("在引用中找到图片")

                            if hasattr(reply_seg, 'url') and reply_seg.url:
                                url = reply_seg.url
                                logger.info(f"✅ 引用中找到图片URL: {url[:100]}...")
                                return url

                            if hasattr(reply_seg, 'base64') and reply_seg.base64:
                                logger.info(f"✅ 引用中找到base64数据")
                                return f"data:image/jpeg;base64,{reply_seg.base64}"

        # 3. 检查@用户（QQ头像）
        for seg in chain:
            if isinstance(seg, At):
                qq = str(seg.qq)
                avatar_url = f"https://q.qlogo.cn/g?b=qq&nk={qq}&s=640"
                logger.info(f"找到@用户，使用QQ头像URL: {avatar_url}")
                return avatar_url

        logger.info("❌ 未找到任何图片数据")
        return ""

        # ---------------- 新增：Base64图片压缩辅助函数 (用于重试) ----------------
    def _resize_base64_image(self, b64_string: str, scale: float = 0.7) -> str:
        """
        将 Base64 图片按比例缩小
        :param scale: 缩放比例 (0.5 表示缩小一半)
        """
        if PyImage is None:
            return b64_string

        try:
            # 1. 清理并解码 Base64
            if "base64," in b64_string:
                header, data = b64_string.split("base64,", 1)
            else:
                header = "data:image/jpeg;base64,"
                data = b64_string

            img_bytes = base64.b64decode(data)

            # 2. Pillow 处理
            img = PyImage.open(io.BytesIO(img_bytes))
            img = img.convert("RGB")

            # 3. 计算新尺寸
            new_width = int(img.width * scale)
            new_height = int(img.height * scale)

            # 限制最小尺寸，防止缩得太小
            if new_width < 256 or new_height < 256:
                return b64_string  # 不再压缩

            img = img.resize((new_width, new_height), PyImage.LANCZOS)

            # 4. 转回 Base64
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=80)  # 同时稍微降低 JPEG 质量
            new_b64_data = base64.b64encode(buffer.getvalue()).decode('utf-8')

            logger.info(f"📉 图片已压缩重试: {img.width}x{img.height} -> {new_width}x{new_height}")
            return f"{header}{new_b64_data}"

        except Exception as e:
            logger.error(f"❌ 图片压缩失败: {e}")
            return b64_string
    # ---------------- 核心：生成逻辑 (带3次自动降质重试机制) ----------------
    async def _generate_image(self, prompt: str, image_base64: str = None, is_image_to_image: bool = False):
        """调用 Flow2API 生成图片 (包含自动降质重试机制)"""

        max_retries = 3  # 最大重试次数
        current_image_b64 = image_base64

        # 构建请求头
        headers = {
            'Authorization': f'Bearer {self.apikey}',
            'Content-Type': 'application/json'
        }

        for attempt in range(max_retries + 1):
            is_retry = attempt > 0
            if is_retry:
                logger.warning(f"🔄 第 {attempt}/{max_retries} 次重试...")

                # 如果是图生图模式，且 Pillow 可用，则进行压缩
                if is_image_to_image and current_image_b64 and PyImage:
                    # 每次重试都将图片缩小至当前的 70%
                    current_image_b64 = self._resize_base64_image(current_image_b64, scale=0.7)
                elif is_image_to_image and not PyImage:
                    logger.warning("⚠️ 未安装 Pillow，无法压缩图片，仅进行普通重试")

            # --- 构建 Payload ---
            # 确保base64格式正确
            if is_image_to_image and current_image_b64:
                # 确保前缀存在
                if not current_image_b64.startswith("data:image/"):
                    # 简单修复
                    if "base64," in current_image_b64:
                        b64_part = current_image_b64.split("base64,")[1]
                        current_image_b64 = f"data:image/jpeg;base64,{b64_part}"
                    else:
                        current_image_b64 = f"data:image/jpeg;base64,{current_image_b64}"

                payload = {
                    "model": self.current_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": current_image_b64,
                                        "detail": "low" if is_retry else "high"  # 重试时降低细节模式，这很关键
                                    }
                                }
                            ]
                        }
                    ],
                    "stream": True
                }
            else:
                # 文生图 Payload
                payload = {
                    "model": self.current_model,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                    "stream": True
                }

            # --- 发送请求 ---
            logger.info(f"📦 发送请求到 API (尝试 {attempt + 1})")
            try:
                async with aiohttp.ClientSession() as session:
                    timeout = aiohttp.ClientTimeout(total=120)  # 2分钟超时
                    async with session.post(self.api_url, json=payload, headers=headers,
                                            timeout=timeout) as response:
                        response_text = await response.text()

                        # 1. 检查 HTTP 状态码
                        if response.status == 200:
                            # === 解析成功逻辑 ===
                            full_content = ""
                            lines = response_text.strip().split('\n')
                            for line in lines:
                                line = line.strip()
                                if line.startswith("data: ") and line != "data: [DONE]":
                                    try:
                                        chunk = json.loads(line[6:])
                                        if chunk and "choices" in chunk and chunk["choices"]:
                                            delta = chunk["choices"][0].get("delta", {})
                                            content_text = delta.get("content", "")
                                            if content_text: full_content += content_text
                                    except:
                                        pass

                            # 提取 URL
                            url_patterns = [r'!\[.*?\]\((https?://[^\s)]+)\)', r'\((https?://[^\s)]+)\)',
                                            r'(https?://[^\s<>"]+)']
                            found_url = None
                            for pattern in url_patterns:
                                urls = re.findall(pattern, full_content, re.IGNORECASE)
                                if urls:
                                    found_url = urls[0]
                                    break

                            if found_url:
                                logger.info(f"✅ 生成成功 (尝试 {attempt + 1}): {found_url[:50]}...")
                                return True, found_url

                            # 如果有http文本但没匹配到正则
                            if "http" in full_content.lower():
                                words = re.split(r'[\s\n\r\t,.;:!?()\[\]{}]+', full_content)
                                for word in words:
                                    if word.lower().startswith(('http://', 'https://')):
                                        cleaned = re.sub(r'[.,;:!?)\]]+$', '', word)
                                        return True, cleaned

                            logger.warning(f"❌ API返回200但未提取到URL: {full_content[:100]}")

                        elif response.status == 401:
                            return False, "❌ API Key 无效或过期"  # 鉴权失败不重试
                        else:
                            logger.error(f"❌ API 请求失败 ({response.status}): {response_text[:200]}")

            except asyncio.TimeoutError:
                logger.error(f"❌ 请求超时 (尝试 {attempt + 1})")
            except Exception as e:
                logger.error(f"❌ 请求异常: {str(e)}")

            # 如果不是最后一次尝试，等待一秒后重试
            if attempt < max_retries:
                await asyncio.sleep(1)

        # 循环结束还没返回 True，说明彻底失败
        return False, f"❌ 多次重试均失败 (已重试{max_retries}次)。\n建议：更换简单的图片或检查网络。"

    # ---------------- 图生图命令 ----------------

    @filter.command("图")
    async def cmd_image_to_image(self, event: AstrMessageEvent):  # <--- 修改1：删除后面所有的参数，只保留 event
        """使用方法: /图生图 描述 (需附带图片)"""

        # <--- 修改2：手动处理字符串，提取命令之后的所有内容
        raw_text = event.message_str.strip()
        # 按最大分割次数1进行分割，这样可以把命令和参数分开，参数中的空格会被保留
        parts = raw_text.split(maxsplit=1)

        if len(parts) < 2:
            yield event.plain_result("⚠️ 请输入描述")
            return

        prompt = parts[1].strip()  # 获取命令后的部分

        if not self.apikey:
            yield event.plain_result("❌ 请先配置 API Key")
            return

        logger.info(f"执行图生图命令: {prompt}")

        # 记录开始时间
        start_time = time.time()

        # 1. 提取图片URL
        image_data = await self._extract_image_url_from_event(event)

        if not image_data:
            yield event.plain_result(
                "❌ 未检测到图片，请:\n"
                "1. 发送图片 + /图生图 描述词\n"
                "2. 引用图片消息 + /图生图 描述词\n"
                "3. @用户 + /图生图 描述词（使用头像）"
            )
            return

        logger.info(f"✅ 提取到图片数据: {image_data[:100]}...")

        # 2. 统一转换为base64格式
        image_base64 = None

        if image_data.startswith("data:image/jpeg;base64,"):
            # 已经是base64格式，直接使用
            image_base64 = image_data
            b64_len = len(image_data.split("base64,")[1]) if "base64," in image_data else len(image_data)
            logger.info(f"✅ 图片已经是base64格式，长度: {b64_len}")
            yield event.plain_result(f"✅ 检测到base64图片 (长度: {b64_len})")
        else:
            # 是URL格式，智能处理 (GIF本地转，其他API转)
            image_base64 = await self._process_image_url(image_data)

            # 检查返回结果是否是调试信息
            if not image_base64:
                yield event.plain_result(
                    f"❌ 图片转换失败\n"
                    f"原因: 第三方API转换失败\n"
                    f"原始URL: {image_data[:200]}..."
                )
                return
            elif "==============" in image_base64 and "调试用" in image_base64:
                # 返回的是调试信息，直接展示给用户
                yield event.plain_result(
                    f"❌ 图片转换失败，以下是调试信息:\n"
                    f"{image_base64}"
                )
                return
            elif not image_base64.startswith("data:image/jpeg;base64,"):
                # 格式不正确
                yield event.plain_result(
                    f"❌ 图片转换失败，返回格式不正确\n"
                    f"返回数据预览: {image_base64[:300]}..."
                )
                return

        # 3. 显示转换信息
        if "base64," in image_base64:
            b64_len = len(image_base64.split("base64,")[1])
            image_info = f"✅ 图片准备完成 (base64长度: {b64_len})"
        else:
            image_info = f"✅ 图片数据准备完成"

        logger.info(f"{image_info}")

        yield event.plain_result(f"{image_info}\n🎨 正在基于图片生成: {prompt[:50]}...")

        # 4. 调用API（传递base64格式），标记为图生图模式
        success, result = await self._generate_image(prompt, image_base64, is_image_to_image=True)

        # 计算总耗时
        end_time = time.time()
        total_time = end_time - start_time

        if success:
            # 获取当前时间
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            success_text = f"✅ 图片生成完成！\n⏱️ 总耗时: {total_time:.2f}秒\n"
            chain = [
                Plain(success_text),
                Image.fromURL(result),
            ]
            yield event.chain_result(chain)
        else:
            # === 👇 改了这里：失败时把 Base64 信息发出来 👇 ===

            # 截取 Base64 头部前 100 个字符
            b64_preview = image_base64[:100] + "..." if image_base64 else "无数据"
            # 获取总长度
            b64_len = len(image_base64) if image_base64 else 0

            debug_msg = (
                f"❌ 图片生成失败 (耗时: {total_time:.2f}秒)\n"
                f"━━━━━━━━━━━━━━\n"
                f"📉 **Base64 数据诊断**:\n"
                f"• 数据长度: {b64_len}\n"
                f"• 数据头部: {b64_preview}\n"
                f"━━━━━━━━━━━━━━\n"
                f"⚠️ **API 报错详情**:\n{result}"
            )
            yield event.plain_result(debug_msg)


    # ---------------- 文生图命令 ----------------
    @filter.command("文")
    async def cmd_text_to_image(self, event: AstrMessageEvent):  # <--- 修改1：删除后面所有的参数，只保留 event
        """使用方法: /文生图 描述"""

        # <--- 修改2：手动处理字符串
        raw_text = event.message_str.strip()
        parts = raw_text.split(maxsplit=1)

        if len(parts) < 2:
            yield event.plain_result("⚠️ 请输入描述")
            return

        prompt = parts[1].strip()

        if not self.apikey:
            yield event.plain_result("❌ 请先配置 API Key")
            return

        logger.info(f"执行文生图命令: {prompt}")

        # 记录开始时间
        start_time = time.time()

        yield event.plain_result(f"🎨 正在生成: {prompt[:50]}...")

        # 文生图不需要图片数据
        success, result = await self._generate_image(prompt, None, is_image_to_image=False)

        # 计算总耗时
        end_time = time.time()
        total_time = end_time - start_time

        if success:
            # 获取当前时间
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            success_text = f"✅ 图片生成完成！\n⏱️ 总耗时: {total_time:.2f}秒\n"
            chain = [
                Plain(success_text),
                Image.fromURL(result),
            ]
            yield event.chain_result(chain)
        else:
            # 失败时也显示耗时
            yield event.plain_result(f"❌ 图片生成失败 (耗时: {total_time:.2f}秒)\n\n错误详情:\n{result}")

    # ---------------- 自定义快捷指令 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_prompt_command(self, event: AstrMessageEvent):
        """处理自定义提示词"""
        text = event.message_str.strip()
        if not text:
            return

        parts = text.split()
        if not parts:
            return

        cmd = parts[0].strip().lstrip("/")
        if cmd not in self.prompt_map:
            return

        if not self.apikey:
            yield event.plain_result("❌ 请先配置 API Key")
            return

        actual_prompt = self.prompt_map[cmd]

        # 记录开始时间
        start_time = time.time()

        # 提取图片数据
        image_data = await self._extract_image_url_from_event(event)

        image_base64 = None
        is_image_mode = False

        if image_data:
            logger.info(f"自定义指令: {cmd}, 找到图片数据")

            # 统一转换为base64格式
            if image_data.startswith("data:image/jpeg;base64,"):
                image_base64 = image_data
                is_image_mode = True
            else:
                # 智能处理 (GIF本地转，其他API转)
                image_base64 = await self._process_image_url(image_data)
                is_image_mode = bool(image_base64) and "失败" not in image_base64

            if is_image_mode:
                yield event.plain_result(f"🎨 执行快捷指令 [{cmd}]... (图生图模式)")
                success, result = await self._generate_image(actual_prompt, image_base64, is_image_to_image=True)
            else:
                yield event.plain_result(f"🎨 执行快捷指令 [{cmd}]... (文生图模式，图片转换失败)")
                success, result = await self._generate_image(actual_prompt, None, is_image_to_image=False)
        else:
            logger.info(f"自定义指令: {cmd}, 无图片数据")
            yield event.plain_result(f"🎨 执行快捷指令 [{cmd}]... (文生图模式)")
            success, result = await self._generate_image(actual_prompt, None, is_image_to_image=False)

        # 计算总耗时
        end_time = time.time()
        total_time = end_time - start_time

        if success:
            # 获取当前时间
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            success_text = f"✅ 图片生成完成！\n⏱️ 总耗时: {total_time:.2f}秒\n"
            chain = [
                Plain(success_text),
                Image.fromURL(result),
            ]
            yield event.chain_result(chain)
        else:
            # 失败时也显示耗时
            yield event.plain_result(f"❌ 图片生成失败 (耗时: {total_time:.2f}秒)\n\n错误详情:\n{result}")

        event.stop_event()

    # ---------------- 自定义快捷指令 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_prompt_command(self, event: AstrMessageEvent):
        """处理自定义提示词"""
        text = event.message_str.strip()
        if not text:
            return

        parts = text.split()
        if not parts:
            return

        cmd = parts[0].strip().lstrip("/")
        if cmd not in self.prompt_map:
            return

        if not self.apikey:
            yield event.plain_result("❌ 请先配置 API Key")
            return

        actual_prompt = self.prompt_map[cmd]

        # 提取图片数据
        image_data = await self._extract_image_url_from_event(event)

        image_base64 = None
        is_image_mode = False

        if image_data:
            logger.info(f"自定义指令: {cmd}, 找到图片数据")

            # 统一转换为base64格式
            if image_data.startswith("data:image/jpeg;base64,"):
                image_base64 = image_data
                is_image_mode = True
            else:
                image_base64 = await self._convert_url_to_base64_via_api(image_data)
                is_image_mode = bool(image_base64)

            if is_image_mode:
                yield event.plain_result(f"🎨 执行快捷指令 [{cmd}]... (图生图模式)")
                success, result = await self._generate_image(actual_prompt, image_base64, is_image_to_image=True)
            else:
                yield event.plain_result(f"🎨 执行快捷指令 [{cmd}]... (文生图模式，图片转换失败)")
                success, result = await self._generate_image(actual_prompt, None, is_image_to_image=False)
        else:
            logger.info(f"自定义指令: {cmd}, 无图片数据")
            yield event.plain_result(f"🎨 执行快捷指令 [{cmd}]... (文生图模式)")
            success, result = await self._generate_image(actual_prompt, None, is_image_to_image=False)

        if success:
            chain = [
                Plain("✅ 图片生成完成！\n"),
                Image.fromURL(result),
            ]
            yield event.chain_result(chain)
        else:
            yield event.plain_result(result)

        event.stop_event()

    # ---------------- 新增：模型管理命令 ----------------
    @filter.command("切换模型")
    async def switch_model(self, event: AstrMessageEvent):
        """切换模型 - 循环切换所有可用模型"""
        current_index = self.available_models.index(
            self.current_model) if self.current_model in self.available_models else 0
        next_index = (current_index + 1) % len(self.available_models)
        self.current_model = self.available_models[next_index]

        # 获取模型信息
        model_info = self._get_model_info(self.current_model)

        response = (
            f"🔄 模型已切换到: {self.current_model}\n\n"
            f"📊 模型信息:\n"
            f"• 名称: {model_info['name']}\n"
            f"• 类型: {model_info['type']}\n"
            f"• 方向: {model_info['orientation']}\n"
            f"• 描述: {model_info['description']}\n\n"
            f"📋 提示: 使用 /当前模型 查看详情，/模型列表 查看所有可用模型"
        )

        yield event.plain_result(response)

    @filter.command("当前模型")
    async def show_current_model(self, event: AstrMessageEvent):
        """显示当前使用的模型详情"""
        model_info = self._get_model_info(self.current_model)

        response = (
            f"🔄 当前使用模型详情:\n\n"
            f"📝 标识符:\n{self.current_model}\n\n"
            f"📊 基本信息:\n"
            f"• 名称: {model_info['name']}\n"
            f"• 类型: {model_info['type']}\n"
            f"• 方向: {model_info['orientation']}\n"
            f"• 描述: {model_info['description']}\n\n"
            f"⚙️ 技术特点:\n"
            f"• 支持: {model_info['capabilities']}\n"
            f"• 推荐用途: {model_info['recommended_use']}"
        )

        yield event.plain_result(response)

    @filter.command("模型列表")
    async def list_models(self, event: AstrMessageEvent):
        """显示所有可用模型"""
        models_info = []

        for i, model in enumerate(self.available_models):
            info = self._get_model_info(model)
            current_marker = " 👈" if model == self.current_model else ""
            models_info.append(
                f"{i + 1}. {model}{current_marker}\n"
                f"   📝 {info['name']} | {info['type']} | {info['orientation']}"
            )

        response = (
                f"📚 可用模型列表 (共{len(self.available_models)}个):\n\n" +
                "\n\n".join(models_info) +
                f"\n\n🔧 使用方法:\n"
                f"• /切换模型 - 切换到下一个模型\n"
                f"• /当前模型 - 查看当前模型详情\n"
                f"• /选择模型 <编号> - 直接选择模型"
        )

        yield event.plain_result(response)

    @filter.command("选择模型")
    async def select_model(self, event: AstrMessageEvent, model_num: int):
        """通过编号选择模型: /选择模型 1"""
        if model_num < 1 or model_num > len(self.available_models):
            yield event.plain_result(f"❌ 请输入 1-{len(self.available_models)} 之间的数字")
            return

        selected_model = self.available_models[model_num - 1]
        self.current_model = selected_model

        model_info = self._get_model_info(selected_model)

        response = (
            f"✅ 已选择模型 #{model_num}: {selected_model}\n\n"
            f"📊 模型信息:\n"
            f"• 名称: {model_info['name']}\n"
            f"• 类型: {model_info['type']}\n"
            f"• 方向: {model_info['orientation']}\n"
            f"• 描述: {model_info['description']}"
        )

        yield event.plain_result(response)

    def _get_model_info(self, model_id: str) -> dict:
        """获取模型详细信息"""
        model_info = {
            "name": "未知模型",
            "type": "未知类型",
            "orientation": "未知方向",
            "description": "无描述",
            "capabilities": "未知",
            "recommended_use": "通用"
        }

        # Gemini 系列
        if "gemini-2.5-flash" in model_id:
            model_info["name"] = "Gemini 2.5 Flash"
            model_info["type"] = "图/文生图"
            model_info["capabilities"] = "快速推理，成本效益高"
            model_info["recommended_use"] = "日常创作，快速响应"
        elif "gemini-3.0-pro" in model_id:
            model_info["name"] = "Gemini 3.0 Pro"
            model_info["type"] = "图/文生图"
            model_info["capabilities"] = "高级推理，高质量输出"
            model_info["recommended_use"] = "专业创作，高质量要求"
        elif "imagen-4.0" in model_id:
            model_info["name"] = "Imagen 4.0"
            model_info["type"] = "图/文生图"
            model_info["capabilities"] = "顶尖图像生成，艺术性强"
            model_info["recommended_use"] = "艺术创作，高质量视觉"

        # 判断方向
        if "landscape" in model_id:
            model_info["orientation"] = "横屏 (16:9)"
            model_info["description"] = "适合风景、场景、宽屏图像"
        elif "portrait" in model_id:
            model_info["orientation"] = "竖屏 (9:16)"
            model_info["description"] = "适合人像、立绘、移动端展示"

        return model_info

    # ---------------- 配置显示命令 ----------------
    @filter.command("gemini设置")
    async def show_settings(self, event: AstrMessageEvent):
        """显示当前设置"""
        key_mask = self.apikey[:4] + "***" + self.apikey[-4:] if len(self.apikey) > 8 else "未配置"

        model_info = self._get_model_info(self.current_model)

        info = (
            f"🎨 Gemini 绘图插件 (纯base64版) v8.6\n\n"
            f"📊 基本设置：\n"
            f"• API地址: {self.api_url}\n"
            f"• API Key: {key_mask}\n"
            f"• 当前模型: {self.current_model}\n"
            f"• 模型名称: {model_info['name']}\n"
            f"• 图像方向: {model_info['orientation']}\n"
            f"• 自定义指令: {len(self.prompt_map)} 个\n\n"
            f"🔄 图片处理流程：\n"
            f"• 转换API: {'✅ 启用' if self.enable_convert_api else '❌ 禁用'}\n"
            f"• 转换地址: {self.convert_api_url}\n"
            f"• 处理流程: URL → 转换API → base64 → 谷歌API\n\n"
            f"🎨 绘图命令：\n"
            f"• /文生图 <描述词>\n"
            f"• /图生图 <描述词>"
        )
        yield event.plain_result(info)

    async def terminate(self):
        """插件卸载时调用"""
        logger.info("Gemini图像生成插件已安全卸载")