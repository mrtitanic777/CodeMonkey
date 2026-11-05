"""Building a project for its target with the platform's own build system, and saying what failed.

An Xbox 360 project is built by MSBuild 4 with the Xbox 360 platform the SDK installs
(MSBuild/Microsoft.Cpp/v4.0/Platforms/Xbox 360): the SDK's compiler, linker and imagexex, with the
settings its own samples use, so a program comes out as an .xex. A project folder without a
complete project file (none, or one that imports none of the build rules) gets one written from the
SDK samples' settings, with every .cpp and .c in the folder; an existing complete one is used as
it is.

build(project_dir, target) -> {"ran", "ok", "errors": [(file, line, code, message)], "outputs", "wrote", "log"}
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import uuid
from typing import Dict, List, Optional

PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
WINDIR = os.environ.get("WINDIR", r"C:\Windows")
MSBUILD4 = os.path.join(WINDIR, "Microsoft.NET", "Framework", "v4.0.30319", "MSBuild.exe")
X360_PLATFORM = os.path.join(PF86, "MSBuild", "Microsoft.Cpp", "v4.0", "Platforms", "Xbox 360")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# the SDK samples' settings (Source/Samples/*/ *2010.vcxproj), without their shared sample framework
X360_LIBS = "d3d9.lib;d3dx9.lib;xgraphics.lib;xapilib.lib;xaudio2.lib;x3daudio.lib;xmcore.lib;xboxkrnl.lib;xbdm.lib"
X360_LIBS_DEBUG = "d3d9d.lib;d3dx9d.lib;xgraphicsd.lib;xapilibd.lib;xaudiod2.lib;x3daudiod.lib;xmcored.lib;xboxkrnl.lib;xbdm.lib"

_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<!-- Written by CodeMonkey from the Xbox 360 SDK samples' settings. -->
<Project DefaultTargets="Build" ToolsVersion="4.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup Label="ProjectConfigurations">
    <ProjectConfiguration Include="Debug|Xbox 360"><Configuration>Debug</Configuration><Platform>Xbox 360</Platform></ProjectConfiguration>
    <ProjectConfiguration Include="Release|Xbox 360"><Configuration>Release</Configuration><Platform>Xbox 360</Platform></ProjectConfiguration>
  </ItemGroup>
  <PropertyGroup Label="Globals">
    <ProjectGuid>{{{guid}}}</ProjectGuid>
    <RootNamespace>{name}</RootNamespace>
    <Keyword>Win32Proj</Keyword>
  </PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.Default.props" />
  <PropertyGroup Condition="'$(Configuration)|$(Platform)'=='Debug|Xbox 360'" Label="Configuration">
    <ConfigurationType>Application</ConfigurationType>
    <UseDebugLibraries>true</UseDebugLibraries>
  </PropertyGroup>
  <PropertyGroup Condition="'$(Configuration)|$(Platform)'=='Release|Xbox 360'" Label="Configuration">
    <ConfigurationType>Application</ConfigurationType>
    <WholeProgramOptimization>false</WholeProgramOptimization>
  </PropertyGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.props" />
  <PropertyGroup>
    <OutDir>$(Configuration)\\</OutDir>
    <IntDir>$(Configuration)\\obj\\</IntDir>
  </PropertyGroup>
  <ItemDefinitionGroup Condition="'$(Configuration)|$(Platform)'=='Debug|Xbox 360'">
    <ClCompile>
      <WarningLevel>Level3</WarningLevel>
      <Optimization>Disabled</Optimization>
      <RuntimeLibrary>MultiThreadedDebug</RuntimeLibrary>
      <ExceptionHandling>false</ExceptionHandling>
      <PreprocessorDefinitions>_DEBUG;_XBOX;XBOX;_XBOX_CRT_DEPRECATE_INSECURE;%(PreprocessorDefinitions)</PreprocessorDefinitions>
      <DebugInformationFormat>ProgramDatabase</DebugInformationFormat>
    </ClCompile>
    <Link>
      <AdditionalDependencies>{libs_debug};%(AdditionalDependencies)</AdditionalDependencies>
      <GenerateDebugInformation>true</GenerateDebugInformation>
    </Link>
  </ItemDefinitionGroup>
  <ItemDefinitionGroup Condition="'$(Configuration)|$(Platform)'=='Release|Xbox 360'">
    <ClCompile>
      <WarningLevel>Level3</WarningLevel>
      <Optimization>Full</Optimization>
      <StringPooling>true</StringPooling>
      <FunctionLevelLinking>true</FunctionLevelLinking>
      <RuntimeLibrary>MultiThreaded</RuntimeLibrary>
      <IntrinsicFunctions>true</IntrinsicFunctions>
      <ExceptionHandling>false</ExceptionHandling>
      <PreprocessorDefinitions>NDEBUG;_XBOX;XBOX;_XBOX_CRT_DEPRECATE_INSECURE;%(PreprocessorDefinitions)</PreprocessorDefinitions>
      <DebugInformationFormat>ProgramDatabase</DebugInformationFormat>
      <BufferSecurityCheck>false</BufferSecurityCheck>
    </ClCompile>
    <Link>
      <AdditionalOptions>/ignore:4089 %(AdditionalOptions)</AdditionalOptions>
      <AdditionalDependencies>{libs};%(AdditionalDependencies)</AdditionalDependencies>
      <GenerateDebugInformation>true</GenerateDebugInformation>
      <OptimizeReferences>true</OptimizeReferences>
      <EnableCOMDATFolding>true</EnableCOMDATFolding>
      <SetChecksum>true</SetChecksum>
    </Link>
  </ItemDefinitionGroup>
  <ItemGroup>
{sources}
  </ItemGroup>
  <Import Project="$(VCTargetsPath)\\Microsoft.Cpp.targets" />
</Project>
"""

_MSG = re.compile(r"^\s*(?:\d+>)?(?P<file>[^(\n]+?)(?:\((?P<line>\d+)(?:,\d+)?\))?\s*:\s*(?:fatal\s+)?error\s+"
                  r"(?P<code>[A-Z]+\d+)\s*:\s*(?P<msg>.*?)(?:\s+\[[^\]]*\.vcxproj\])?\s*$", re.M)


def x360_available() -> bool:
    return os.path.isfile(MSBUILD4) and os.path.isdir(X360_PLATFORM)


def _complete(path: str) -> bool:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            return "Microsoft.Cpp.targets" in fh.read()
    except OSError:
        return False


def project_file(project_dir: str, name: Optional[str] = None, extra: Optional[List[str]] = None) -> tuple:
    """(the .vcxproj to build, whether it was written now). A stub is replaced; a complete one kept.
    `extra`: more source files for a new project file, relative to the folder (they may be outside it,
    e.g. ../core/game.c), compiled where they are."""
    found = sorted(glob.glob(os.path.join(project_dir, "*.vcxproj")))
    complete = [p for p in found if _complete(p)]
    if complete:
        return complete[0], False
    target = found[0] if found else os.path.join(project_dir, (name or os.path.basename(project_dir) or "App") + ".vcxproj")
    sources = sorted(os.path.relpath(p, project_dir) for ext in ("*.cpp", "*.c")
                     for p in glob.glob(os.path.join(project_dir, "**", ext), recursive=True)
                     if not os.path.relpath(p, project_dir).lower().startswith(("release", "debug")))
    sources += [e for e in (extra or []) if e not in sources]
    items = "\n".join(f'    <ClCompile Include="{s}" />' for s in sources)
    text = _TEMPLATE.format(guid=str(uuid.uuid4()).upper(), name=os.path.splitext(os.path.basename(target))[0],
                            libs=X360_LIBS, libs_debug=X360_LIBS_DEBUG, sources=items)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    return target, True


def build(project_dir: str, target: Dict, configuration: str = "Release", timeout: int = 900,
          extra: Optional[List[str]] = None) -> Dict:
    if target.get("platform") != "xbox360":
        return {"ran": False, "ok": False, "errors": [], "outputs": [], "wrote": None,
                "log": "code_build builds Xbox 360 projects; for other targets use the project's own build "
                       "(code_compile checks a single file)"}
    if not x360_available():
        return {"ran": False, "ok": False, "errors": [], "outputs": [], "wrote": None,
                "log": "the Xbox 360 build platform for MSBuild 4 is not installed here"}
    proj, wrote = project_file(project_dir, extra=extra)
    if configuration not in ("Debug", "Release"):
        configuration = "Release"
    env = dict(os.environ)
    try:
        done = subprocess.run([MSBUILD4, os.path.basename(proj), f"/p:Configuration={configuration}",
                               "/p:Platform=Xbox 360", "/nologo", "/v:minimal", "/m:1"],
                              cwd=project_dir, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "errors": [], "outputs": [], "wrote": proj if wrote else None,
                "log": f"the build took more than {timeout} s"}
    log = done.stdout + done.stderr
    errors, seen = [], set()
    for m in _MSG.finditer(log):
        key = (m.group("file").strip(), m.group("line"), m.group("code"), m.group("msg").strip())
        if key not in seen:
            seen.add(key)
            f = key[0]
            if os.path.isabs(f) and f.lower().startswith(os.path.abspath(project_dir).lower()):
                f = os.path.relpath(f, project_dir)
            errors.append((f, int(key[1]) if key[1] else 0, key[2], key[3]))
    out_dir = os.path.join(project_dir, configuration)
    outputs = sorted(os.path.relpath(p, project_dir) for p in glob.glob(os.path.join(out_dir, "*.xex"))
                     + glob.glob(os.path.join(out_dir, "*.exe")))
    return {"ran": True, "ok": done.returncode == 0 and not errors, "errors": errors[:40], "outputs": outputs,
            "wrote": os.path.relpath(proj, project_dir) if wrote else None, "log": log[-4000:]}


__all__ = ["build", "project_file", "x360_available"]
