cmake_minimum_required(VERSION 3.22)
project(execution_engine CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Python3_ROOT_DIR and pybind11_DIR are passed in by build.ps1 at configure
# time so they point at the active .venv — no hardcoded paths here.
find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
find_package(pybind11 CONFIG REQUIRED)

# ── execution_engine module ───────────────────────────────────────────────────
pybind11_add_module(execution_engine src/execution_engine.cpp)
target_include_directories(execution_engine PRIVATE include)
target_compile_features(execution_engine PRIVATE cxx_std_20)
