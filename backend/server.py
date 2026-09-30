import json
import os
import time  # Подключаем библиотеку для пауз
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types

# Подключаем логику партнера
from engineering.rag.index import KnowledgeBase
from engineering.rag.retriever import retrieve

# Инициализируем базу знаний при старте сервера
kb = KnowledgeBase.load()

app = FastAPI()

# Берем ключ из безопасного хранилища переменных окружения
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

# Allow the frontend to talk to this server (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. The Rulebook
system_instruction = """
You are an expert mechanical engineer and SolidWorks Python API developer.
Write a Python script using win32com.client to automate SolidWorks based on the user's parameters and image.
CRITICAL RULE: Output ONLY valid Python code. Do NOT wrap the code in markdown blocks (do not use ```python).
"""

# 3. The API Endpoint
@app.post("/generate")
async def generate_script(data: str = Form(...), image: UploadFile = File(None)):
    
    params = json.loads(data)
    user_text = params.get('prompt', '').strip() # Безопасно получаем текст
    
    # 1. Ищем инструкции в базе знаний ТОЛЬКО если есть текст
    if user_text:
        try:
            rag_data = retrieve(kb=kb, message=user_text, domain="statics")
            rag_context = rag_data.as_prompt_block()
        except Exception as e:
            print(f"⚠️ Ошибка RAG (вероятно лимит 429). Продолжаем без базы знаний. Ошибка: {e}")
            rag_context = "No additional context available."
    else:
        # Если текста нет, пропускаем поиск, чтобы избежать ошибки 400
        rag_context = "No text description provided. Rely strictly on the attached image for geometry and constraints."
        user_text = "Please analyze the attached image and parameters."

    # 2. Собираем промпт с учетом найденной информации
    user_prompt = f"""
    Create a SolidWorks Python script using these exact parameters.
    
    [KNOWLEDGE BASE CONTEXT]
    {rag_context}
    [/KNOWLEDGE BASE CONTEXT]

    - Problem Description: {user_text}
    - Material: {params['material']['name']} (Yield={params['material']['yieldStrengthMPa']} MPa)
    - Geometry: Length={params['geometry']['lengthM']}m
    - Force: {params['loads']['forceN']} N
    """
    
    contents = [user_prompt]

    # If the user uploaded an image, attach it!
    if image:
        image_bytes = await image.read()
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=image.content_type)
        contents.append(image_part)

    # 3. Запрос к Gemini с умными повторами (обход лимитов)
    max_retries = 3
    response = None
    error_msg = ""
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_instruction)
            )
            break  # Запрос успешен, выходим из цикла
            
        except Exception as e:
            error_msg = str(e)
            print(f"⚠️ Попытка {attempt + 1}/{max_retries} не удалась: {error_msg}")
            
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                print("⏳ Лимит запросов (429). Сервер ждет 65 секунд...")
                time.sleep(65)
            elif "503" in error_msg or "UNAVAILABLE" in error_msg:
                print("⏳ Серверы Google перегружены (503). Сервер ждет 15 секунд...")
                time.sleep(15)
            elif "400" in error_msg:
                print("❌ Ошибка 400 (пустой файл/неверные данные). Прерываем.")
                break  # Нет смысла повторять 400 ошибку
            else:
                time.sleep(5)  # Неизвестная ошибка, ждем 5 секунд

    # Если после всех попыток ответа так и нет
    if not response:
        print(f"\n🚨 CRASH REPORT: Не удалось получить ответ. Последняя ошибка: {error_msg}\n")
        status = 503 if "503" in error_msg or "429" in error_msg else 500
        user_message = "# Сервер нейросети временно перегружен или лимит исчерпан. Пожалуйста, подождите немного и нажмите Generate снова."
        
        return JSONResponse(
            status_code=status,
            content={"code": user_message, "parsed": {}}
        )

    # 4. Обработка успешного ответа
    clean_code = response.text.replace("```python", "").replace("```", "").strip()
    
    response_data = {
        "code": clean_code,
        "parsed": {
            "lengthM": params['geometry']['lengthM'],
            "widthM": params['geometry']['widthM'],
            "heightM": params['geometry']['heightM'],
            "fixture": params['loads']['fixture'],
            "forceN": params['loads']['forceN'],
            "materialName": params['material']['name'],
            "yieldStrengthMPa": params['material']['yieldStrengthMPa']
        }
    }
    
    return JSONResponse(content=response_data)

if __name__ == "__main__":
    import uvicorn
    # Starts the server on port 8000
    uvicorn.run(app, host="127.0.0.1", port=8000)
