from fastapi import FastAPI, File, UploadFile
import asyncio
import uuid
import base64
import os
from openai import OpenAI  # 需要 pip install openai

app = FastAPI()
tasks = {}
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))  # Render 环境变量里配好

class ScreenshotTask:
    def __init__(self, task_id):
        self.task_id = task_id
        self.event = asyncio.Event()
        self.image_bytes = None  # 临时存一下，等AI读完就删
        self.analysis_result = None
        self.status = "WAITING"

# ---------- 1. 伴侣调用的 MCP 工具（挂起等待文字结果） ----------
@app.get("/mcp/request_screen")
async def request_screen():
    task_id = str(uuid.uuid4())
    task = ScreenshotTask(task_id)
    tasks[task_id] = task
    
    try:
        # 挂起最多 55 秒（留给 iPhone 截图 + AI 分析）
        await asyncio.wait_for(task.event.wait(), timeout=55.0)
    except asyncio.TimeoutError:
        tasks.pop(task_id, None)
        return {"status": "timeout", "message": "iPhone 或 AI 响应超时"}
    
    # 被唤醒，说明 AI 已经分析完了
    result = task.analysis_result
    tasks.pop(task_id, None)  # 彻底清理任务
    
    # 返回纯文字给伴侣（聊天界面只显示这段话）
    return {"status": "success", "analysis": result}

# ---------- 2. iPhone 截完图传上来（二进制图片） ----------
@app.post("/iphone/upload/{task_id}")
async def iphone_upload(task_id: str, file: UploadedFile = File(...)):
    task = tasks.get(task_id)
    if not task:
        return {"status": "task_not_found"}
    
    # 1. 读取图片二进制到内存
    image_bytes = await file.read()
    
    # 2. 转成 Base64（OpenAI/Claude 都认这个格式）
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    # 3. 图片已经转完字符串了，原始二进制可以立刻删掉（释放内存）
    del image_bytes  # 手动释放大内存
    
    # 4. 调用 AI 视觉接口分析（这里以 OpenAI GPT-4o 为例）
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # 便宜又快
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请用简短中文描述这张手机截图里显示的核心内容，比如正在哪个App、有什么关键文字。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }
                    ]
                }
            ],
            max_tokens=300
        )
        analysis = response.choices[0].message.content
    except Exception as e:
        analysis = f"AI 分析失败: {str(e)}"
    
    # 5. **** Base64 字符串也用完了，立刻删掉 ****
    del base64_image
    
    # 6. 把分析结果存进 task
    task.analysis_result = analysis
    task.status = "DONE"
    
    # 7. 唤醒上面那个挂起的 /mcp/request_screen
    task.event.set()
    
    return {"status": "analysis_done"}

# ---------- 3. （可选）iPhone 轮询查任务 ----------
@app.get("/iphone/poll/{task_id}")
async def iphone_poll(task_id: str):
    task = tasks.get(task_id)
    if not task or task.status != "WAITING":
        return {"action": "idle"}
    return {"action": "screenshot_now"}