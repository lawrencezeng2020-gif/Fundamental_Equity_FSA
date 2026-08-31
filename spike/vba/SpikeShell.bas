Attribute VB_Name = "SpikeShell"
'===============================================================================
' SpikeShell.bas -- Phase 0 macOS sandbox risk spike
'
' PURPOSE
'   Answer one narrow question before any real work is built on top of it
'   (PLANNING.md Section 7): can VBA Shell() in Excel for Mac launch our
'   Python CLI, and can Excel then open a workbook file that Python wrote to
'   a normal user directory OUTSIDE Excel's App Sandbox container?
'
'   This is the reference implementation the real GenerateModel.bas (Phase 6)
'   will be built from. Every step here maps 1:1 onto a step the real macro
'   will need: build an absolute command line, Shell() it, wait for the
'   output file (Shell is ASYNCHRONOUS -- it returns immediately, it does not
'   wait for the child process), grant sandbox file access, open the result,
'   and report success/failure to the user instead of letting a raw VBA
'   error dialog appear.
'
' HOW TO RUN THIS (see spike/README.md for the full walkthrough)
'   1. Open Excel for Mac, create/open any workbook.
'   2. Tools > Macro > Visual Basic Editor (or Option-F11).
'   3. File > Import File... and choose this .bas file.
'   4. Back in Excel, fill in three absolute-path input cells on Sheet1:
'        B1: path to your venv's python, e.g.
'            /Users/you/work/Fundamental_Equity_FSA/venv/bin/python
'        B2: path to spike_target.py, e.g.
'            /Users/you/work/Fundamental_Equity_FSA/spike/spike_target.py
'        B3: an absolute output directory, e.g.
'            /Users/you/Documents/FSA_Output_spike
'      (All three must be absolute paths -- see spike_target.py's own
'      docstring for why: it must run correctly with no PATH/profile/cwd.)
'   5. Run RunSpike (Tools > Macro > Macros... > RunSpike > Run).
'   6. Read the status written to Sheet1!B5. See README.md for PASS/FAIL
'      criteria.
'
' WHY THIS CANNOT BE FULLY AUTOMATED FROM OUTSIDE EXCEL
'   Running a VBA macro requires Excel's own macro-security prompts and the
'   VB Editor's import step, both of which are interactive by design (macOS
'   Gatekeeper / Office "Enable Macros" dialogs cannot be scripted around,
'   nor should they be). The command-line half of this spike (spike_target.py
'   under an emptied environment) and the AppleScript-recalculation half were
'   verified independently and automatically; this macro is what proves the
'   remaining, Excel-sandbox-specific half, and that final button press is a
'   manual step for the repo owner.
'===============================================================================

Option Explicit

' Poll the filesystem for up to this many seconds waiting for spike_target.py
' to finish writing the output file. Shell() on macOS Excel is asynchronous:
' the line after Shell() executes immediately, before the child process has
' necessarily even started, let alone finished. There is no built-in
' "Shell and wait" in Excel-for-Mac VBA, so polling for the expected output
' file is the standard workaround.
Const SPIKE_TIMEOUT_SECONDS As Double = 30
Const SPIKE_POLL_INTERVAL_SECONDS As Double = 0.5
Const SPIKE_OUTPUT_FILENAME As String = "spike_output.xlsx"

Sub RunSpike()

    Dim pythonPath As String
    Dim scriptPath As String
    Dim outputDir As String
    Dim outputFile As String
    Dim shellCommand As String
    Dim statusCell As Range
    Dim startTime As Double
    Dim elapsed As Double
    Dim fileReady As Boolean

    On Error GoTo ErrorHandler

    Set statusCell = ThisWorkbook.Sheets(1).Range("B5")
    statusCell.Value = "RUNNING..."

    ' --- 1. Read inputs -----------------------------------------------------
    pythonPath = Trim(ThisWorkbook.Sheets(1).Range("B1").Value)
    scriptPath = Trim(ThisWorkbook.Sheets(1).Range("B2").Value)
    outputDir = Trim(ThisWorkbook.Sheets(1).Range("B3").Value)

    If pythonPath = "" Or scriptPath = "" Or outputDir = "" Then
        statusCell.Value = "FAIL: fill in B1 (python path), B2 (spike_target.py path) and B3 (output dir) first -- all absolute paths."
        Exit Sub
    End If

    outputFile = outputDir & "/" & SPIKE_OUTPUT_FILENAME

    ' Remove any stale output from a previous run so we don't mistake an old
    ' file for evidence that THIS run succeeded.
    If FileExists(outputFile) Then
        Kill outputFile
    End If

    ' --- 2. Build and launch the command -------------------------------------
    ' Absolute paths only -- do not rely on PATH, a shell profile, or cwd.
    ' This mirrors exactly how the real GenerateModel.bas will invoke
    ' `python -m fsa.cli ...`: quote all three paths defensively in case any
    ' of them contain spaces (e.g. "/Users/jane doe/...").
    shellCommand = """" & pythonPath & """ """ & scriptPath & """ """ & outputDir & """"

    Shell (shellCommand)

    ' --- 3. Poll for the output file (Shell() is asynchronous) --------------
    startTime = Timer
    fileReady = False
    Do
        If FileExists(outputFile) Then
            fileReady = True
            Exit Do
        End If
        elapsed = Timer - startTime
        If elapsed > SPIKE_TIMEOUT_SECONDS Then
            Exit Do
        End If
        Sleep SPIKE_POLL_INTERVAL_SECONDS
    Loop

    If Not fileReady Then
        statusCell.Value = "FAIL: timed out after " & SPIKE_TIMEOUT_SECONDS & _
            "s waiting for " & outputFile & ". Shell() may not have launched " & _
            "the process, or the sandbox blocked the write. See README.md fallback."
        Exit Sub
    End If

    ' --- 4. Grant sandbox access to the output path, then open it -----------
    ' GrantAccessToMultipleFiles is the sandboxed-Office API for extending
    ' file access beyond what the App Sandbox would otherwise allow. Without
    ' it, Workbooks.Open below may silently fail or prompt unexpectedly for
    ' a path outside the sandbox container (PLANNING.md Section 7).
    Dim grantResult As Boolean
    grantResult = Application.GrantAccessToMultipleFiles(Array(outputFile))

    If Not grantResult Then
        statusCell.Value = "FAIL: GrantAccessToMultipleFiles returned False for " & outputFile
        Exit Sub
    End If

    Dim wb As Workbook
    Set wb = Workbooks.Open(outputFile)

    ' --- 5. Success -----------------------------------------------------------
    statusCell.Value = "PASS: opened " & outputFile & " written by " & _
        wb.Sheets(1).Range("B3").Value & " at " & Now()

    Exit Sub

ErrorHandler:
    statusCell.Value = "FAIL: VBA error " & Err.Number & " - " & Err.Description
End Sub

' --- Helpers ----------------------------------------------------------------

Private Function FileExists(ByVal path As String) As Boolean
    On Error Resume Next
    FileExists = (Dir(path) <> "")
    On Error GoTo 0
End Function

' Excel for Mac VBA has no native Sleep; Application.Wait's granularity and
' cross-version behavior on Mac is unreliable for sub-second polling, so we
' busy-wait using Timer instead. Fine for a spike; a production polling loop
' (Phase 6) may want a coarser interval and a maximum-attempts cap.
Private Sub Sleep(ByVal seconds As Double)
    Dim target As Double
    target = Timer + seconds
    Do While Timer < target
        DoEvents
    Loop
End Sub
