import json
import os
import asyncio
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types

# Import the partner's logic
from engineering.rag.index import KnowledgeBase
from engineering.rag.retriever import retrieve

# Initialize the knowledge base on server startup
kb = KnowledgeBase.load()

app = FastAPI()

# Get the key from the secure environment variables
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

# 2. THE RULEBOOK
system_instruction = """
You are an expert mechanical engineer and SolidWorks Python API developer.
CRITICAL RULES:
1. Output ONLY valid Python code using win32com.client. Do NOT wrap the code in markdown blocks (do not use ```python).
2. The [KNOWLEDGE BASE CONTEXT] contains EXAMPLES. DO NOT solve the example problems! Ignore any dimensions, materials, or forces mentioned in the knowledge base. Use it ONLY to understand the API syntax.
3. You MUST extract all dimensions, forces, and material properties EXCLUSIVELY from the [ACTUAL USER TASK] block.
"""

# 3. The API Endpoint
@app.post("/generate")
async def generate_script(data: str = Form(...), image: UploadFile = File(None)):
    
    params = json.loads(data)
    user_text = params.get('prompt', '').strip()
    
    # 1. Search for instructions in the knowledge base
    if user_text:
        try:
            rag_data = retrieve(kb=kb, message=user_text, domain="statics")
            rag_context = rag_data.as_prompt_block()
        except Exception as e:
            print(f"⚠️ RAG Error. Error: {e}")
            rag_context = "No additional context available."
    else:
        rag_context = "No text description provided. Rely strictly on the attached image."
        user_text = "Please analyze the attached image."


    user_prompt = f"""
    [KNOWLEDGE BASE CONTEXT (FOR API SYNTAX REFERENCE ONLY)]
    {rag_context}
    [/KNOWLEDGE BASE CONTEXT]

    =========================================
    [ACTUAL USER TASK - SOLVE THIS EXACT PROBLEM]
    =========================================
    - Problem Description: {user_text}
    - Material Name: {params['material']['name']}
    - Material Properties: Yield = {params['material']['yieldStrengthMPa']} MPa, Young's Modulus = {params['material']['youngsModulusGPa']} GPa, Poisson = {params['material']['poissonsRatio']}
    - Geometry: Length = {params['geometry']['lengthM']} m, Width = {params['geometry']['widthM']} m, Height = {params['geometry']['heightM']} m
    - Loads: Fixture Type = {params['loads']['fixture']}, Force = {params['loads']['forceN']} N
    - Mesh Quality: {params['mesh']}
    =========================================
    
    Write the SolidWorks Python script for the [ACTUAL USER TASK]. Use ONLY the numerical values and conditions listed in the block above.
    """
    
    contents = [user_prompt]

    if image:
        image_bytes = await image.read()
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=image.content_type)
        contents.append(image_part)

    # 3. Request to Gemini
    max_retries = 3
    response = None
    error_msg = ""
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.1
                )
            )
            break
            
        except Exception as e:
            error_msg = str(e)
            print(f"⚠️ Attempt {attempt + 1}/{max_retries} failed: {error_msg}")
            
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                print("⏳ Rate limit (429). Waiting 65 seconds...")
                await asyncio.sleep(65)
            elif "503" in error_msg or "UNAVAILABLE" in error_msg:
                print("⏳ Servers overloaded (503). Waiting 15 seconds...")
                await asyncio.sleep(15)
            elif "400" in error_msg:
                break
            else:
                await asyncio.sleep(5)

    if not response:
        status = 503 if "503" in error_msg or "429" in error_msg else 500
        user_message = "# API limits exhausted or servers overloaded. Please wait a moment and try again."
        return JSONResponse(status_code=status, content={"code": user_message, "parsed": {}})

    # 4. Process the successful response
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
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
