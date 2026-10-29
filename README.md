
# Statica 🏗️

> An AI-powered platform that converts natural language mechanical statics problems into executable SolidWorks 2024 Python macros.

Statica bridges the gap between textbook problem descriptions and complex CAD simulation setups. By leveraging Google's Gemini API and a custom Retrieval-Augmented Generation (RAG) pipeline, Statica acts as an intelligent engineering assistant that interprets geometry, materials, and loads, and generates ready-to-run SolidWorks COM API scripts.

## ✨ Key Features
* **Natural Language to CAD**: Describe your statics problem in plain English or upload a photo of a textbook problem.
* **Intelligent Parameter Extraction**: The AI acts as a filter, prioritizing user text over default form values to build an accurate simulation plan.
* **Dynamic SVG Schematics**: Instantly visualize the parsed problem (e.g., horizontal bending vs. vertical tension) directly in the browser.
* **Pre-Simulation Stress Check**: Perform quick hand-calculations (e.g., beam bending stress $\sigma = \frac{M \cdot c}{I}$ or axial stress $\sigma = \frac{F}{A}$) before ever opening SolidWorks.
* **Safe Code Generation**: Generates deterministic, validated Python scripts using tested SolidWorks COM helpers—the LLM never writes raw SolidWorks code blindly.

## 📖 Documentation
For a deep dive into the system architecture, API constraints, and our RAG implementation, please read the [Statica arc42 Architecture Documentation](docs/architecture_arc42.md).

## 🛠️ Architecture Overview
Statica is divided into three main components:
1. **Frontend (Netlify)**: A static web application (HTML/CSS/JS) that collects user inputs, renders dynamic SVG schematics, and performs preliminary stress checks.
2. **Backend (Render)**: A FastAPI REST server that handles rate-limiting (429/503) asynchronously, queries the Gemini API with strict JSON enforcement, and manages the RAG knowledge base.
3. **Simulation Engine (Local)**: A generated Python script utilizing `pywin32` to drive SolidWorks 2024 locally, applying materials, fixtures, and forces, running the mesh, and extracting results.

## 🚀 Getting Started

### Running a Generated Script
To run a generated SolidWorks simulation on your local machine:
1. Ensure you have **SolidWorks 2024** and **Python 3.12+** installed on a Windows machine.
2. Install the required Windows COM bridge:
   ```bash
   pip install pywin32

```

3. Open SolidWorks 2024 and wait for it to fully load. Save and close any active work.
4. Run the downloaded script:
```bash
python solidworks_simulation.py

```


5. Watch the part being built, meshed, and solved automatically!

## ⚠️ Disclaimer

*Generated scripts interact directly with the SolidWorks COM API. Always review the generated Python script before execution. The meshes are intentionally kept coarse by default to protect shared school/lab PCs.*

```

```
