# JFastFramework
JFastFramework - Descripción

JFastFramework
==============

**JFastFramework** es un framework experimental basado en **FastAPI** diseñado para gestionar tanto un monolito modular como una arquitectura de microservicios.

Arquitectura del Proyecto
-------------------------

![Arquitectura del proyecto](https://miro.medium.com/v2/resize:fit:640/format:webp/0*KikwdypTj1FVSpB2.png)

Características Principales
---------------------------

*   Modularidad: Facilita la separación de lógica en módulos independientes.
*   Escalabilidad: Permite trabajar con microservicios o en un solo monolito.
*   Rendimiento: Basado en FastAPI para optimizar la gestión de peticiones HTTP.
*   Compatibilidad con Bases de Datos: Soporta diversas bases de datos mediante configuraciones flexibles.
*   Gestión Automática de Rutas: Cada módulo tiene su propio sistema de rutas.

Estructura del Proyecto
-----------------------

JFastFramework organiza el código en varias capas para una mejor separación de responsabilidades:

*   **controllers/**: Maneja las peticiones HTTP y las respuestas.
*   **models/**: Define los modelos de datos.
*   **routes/**: Gestiona la configuración de rutas para cada módulo.
*   **services/**: Contiene la lógica de negocio.
*   **repositories/**: Maneja la interacción con la base de datos.

Ejemplo de Código
-----------------

        from fastapi import APIRouter
        from controllers.example\_controller import ExampleController
        
        router = APIRouter()
        controller = ExampleController()
        
        @router.get("/")
        def get\_example():
            return controller.get\_example()
