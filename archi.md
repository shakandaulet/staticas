Statica Project Architectural Documentation (arc42 Methodology)



1. Introduction and Goals



Problem Statement and Significance: Engineering students and professionals allocate substantial time to manually constructing models and configuring calculations in SolidWorks. Often, the available input is limited to textual descriptions or textbook images. The Statica project addresses this inefficiency by serving as an interface between natural language processing and the complex SolidWorks Python API.



Primary Goal: To develop an AI-driven web application capable of converting textual descriptions and images of statics problems into executable Python scripts for SolidWorks 2024.



2. Architecture Constraints



- Infrastructure Constraints: Utilization of the Render free tier for the backend, which imposes limitations on port binding and includes server sleep periods during inactivity.

- API Constraints: Strict quotas applied by the Google Gemini API free tier (maximum of 20 requests per day per project, stringent RPM/TPM limits, and frequent 503 High Demand errors).

- SolidWorks Technical Constraints: Inability to execute SolidWorks scripts directly in cloud environments. Scripts must be executed locally on a user's Windows machine via the win32com library.



3. Context and Scope



The Statica system interacts with the following external actors and systems:



- User (Engineer/Student): Inputs parameters, uploads images, and receives the generated .py file.

- Google Gemini API (LLM): The core engine for natural language processing (NLP) and prompt-based code generation.

- Local SolidWorks Environment (External System): The target execution environment where the user will run the generated macro for simulation (Static Analysis).



4. Solution Strategy



- Frontend: A static web application utilizing HTML, CSS, and JavaScript, deployed on Netlify. Features real-time rendering of dynamic SVG schematics based on AI responses.

- Backend: A RESTful API built with Python and FastAPI, deployed on Render.

- Code Generation: Implementation of Retrieval-Augmented Generation (RAG). The backend retrieves syntax examples from a local vector knowledge base and supplies them to Gemini alongside form data.

- Conflict Resolution (Text vs. Form): Gemini functions as an intelligent filter. If user-provided text conflicts with form data, the AI prioritizes the text and enforces a JSON response containing the corrected parameters.



5. Building Block View



The system is divided into two primary levels:



Level 1: Frontend (Client-side):

The frontend consists of three pages served as six static files:

- 


Level 2: Backend (FastAPI Server):

- server.py: The single entry point utilizing the /generate endpoint.

- KnowledgeBase (RAG Module): Local storage of embeddings providing win32com API syntax context.



6. Runtime View



Scenario: User requests script generation



1. The user inputs form data, adds a textual problem description, and initiates the "Generate" action.

2. The frontend transmits a multipart/form-data payload to the /generate endpoint.

3. The backend extracts the text, queries the local RAG database, and constructs the prompt.

4. The backend calls the Gemini API with the response_mime_type="application/json" parameter enforced.

5. In the event of server overload (503) or quota exhaustion (429), the backend initiates an asynchronous retry loop.

6. Gemini returns a strictly formatted JSON object containing the script code and the recognized parameters.

7. The backend relays the data to the client, and the frontend updates the schematic representation accordingly (e.g., replacing a horizontal beam with a vertical one if axial tension is detected).



7. Deployment View



- Frontend: Deployed via Netlify utilizing continuous deployment from GitHub.

- Backend: Deployed as a Web Service on Render (Docker/Python Environment). Execution is managed via uvicorn with dynamic port binding:



uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))



8. Crosscutting Concepts



8.1. Quota and Network Error Management

Issue: The Google API frequently returned 503 (Unavailable) and 429 (Resource Exhausted) errors, causing backend failures.

Resolution: Implementation of the asyncio module to create an exponential backoff loop that does not block other server processes.



for attempt in range(max_retries):

    try:

        response = client.models.generate_content(...)

        break

    except Exception as e:

        if "429" in error_msg:

            await asyncio.sleep(65)

        elif "503" in error_msg:

            await asyncio.sleep(15)



8.2. Enforced Structured Output (JSON Enforcing)

Issue: The LLM demonstrated a tendency to hallucinate and solve examples from the RAG database instead of the user's defined problem.

Resolution: Strict prompt engineering implementation. Instructions specify that the RAG context is strictly for syntax reference. The Gemini configuration enforces JSON output to maintain communication integrity with the frontend visualization logic.



config=types.GenerateContentConfig(

    system_instruction="Output strictly valid JSON. Override FORM inputs with TEXT inputs.",

    temperature=0.1,

    response_mime_type="application/json"

)



9. Architectural Decisions



- ADR-1: Adoption of FastAPI over Flask. Selected for its native asynchronous support, which is critical for managing LLM response latency, and its overall performance capabilities.

- ADR-2: Exclusion of Server-Side SolidWorks Execution. Due to the high resource requirements of CAD systems, the backend delegates simulation execution entirely to the user's local hardware rather than attempting costly cloud-based simulation.

- ADR-3: Frontend SVG Integration. Rather than requiring the AI to generate image files, the AI generates numerical parameters (JSON). The frontend utilizes JavaScript to render accurate and predictable SVG models based on established strength of materials formulas.



10. Quality Requirements



- Accuracy: The generated Python code must execute successfully in SolidWorks without throwing SyntaxError or COM Error exceptions.

- Resilience: The system must intercept and appropriately handle external API failures.

- Usability: The interface must provide the user with preliminary hand-calculated stress estimates prior to running the simulation.



11. Risks and Technical Debt



- SolidWorks API Volatility Risk: Updates to the COM interface in future Dassault Systemes releases may deprecate the local RAG knowledge base.

- Quota Vulnerability: Reliance on the free-tier Gemini API imposes a hard limit on system scalability (e.g., 20 requests per day).

- Recommendations for Future Improvement:

  1. Transition to a Pay-As-You-Go billing model for the Gemini API.

  2. Implement backend request caching (e.g., via Redis) to prevent querying the LLM for duplicate standard tasks.

  3. Expand the RAG knowledge base to support thermal and frequency analysis modules.



12. Glossary



- RAG (Retrieval-Augmented Generation): An architectural pattern that retrieves relevant contextual data to augment the prompt provided to a large language model.

- win32com: A Python library facilitating interaction with Windows COM objects, utilized here to interface programmatically with SolidWorks.

- Stress Check: A preliminary manual calculation of material stress (e.g., stress = M * c / I or stress = F / A) executed on the frontend to validate AI output parameters.
