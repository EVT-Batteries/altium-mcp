// Isolated DelphiScript sandbox used by the run_altium_script MCP tool.
//
// This is deliberately a SEPARATE script project from Altium_API: a compile
// error or crash in user-supplied script must never break the working MCP
// tooling.
//
// SandboxLog() flushes to disk on every call, so when a script dies silently
// (Altium leaves it paused in the debugger with no dialog) the log still shows
// the last step that completed - the statement after it is the culprit.

const
    REPLACEALL = 1;

var
    LogLines : TStringList;
    LogPath  : String;
    OutPath  : String;
    // Scratch variables: DelphiScript has no inline declarations, so scripts
    // passed to the tool reuse these rather than declaring their own.
    S1, S2, S3 : String;
    I1, I2, I3 : Integer;
    B1         : Integer;
    Obj1, Obj2, Obj3, Obj4, Obj5 : IDispatch;
    List1      : TStringList;
    IntMan     : IIntegratedLibraryManager;
    DbDoc      : IDatabaseLibDocument;

procedure SandboxLog(Msg: String);
begin
    LogLines.Add(Msg);
    LogLines.SaveToFile(LogPath);
end;

procedure Run;
var
    ResultText : String;
    OutLines   : TStringList;
begin
    LogPath := 'C:\Users\Public\altium_mcp\sandbox_log.txt';
    OutPath := 'C:\Users\Public\altium_mcp\sandbox_result.json';
    LogLines := TStringList.Create;
    ResultText := '{"sandbox": "no result set"}';
    SandboxLog('sandbox start');

    try
        // === BEGIN EXPERIMENT (rewritten by the run_altium_script tool) ===
        Obj2 := SchServer.SchObjectFactory(82, 1);
        SandboxLog('Checking wire name/comment properties');
        if Obj2.Comment <> nil then
            SandboxLog('Obj2.Comment: ' + Obj2.Comment.Text)
        else
            SandboxLog('Obj2.Comment is nil');

        if Obj2.Description <> nil then
            SandboxLog('Obj2.Description: ' + Obj2.Description.Text)
        else
            SandboxLog('Obj2.Description is nil');

        // Check child parameters
        List1 := TStringList.Create;
        Iter := Obj2.SchIterator_Create;
        Iter.AddFilter_ObjectSet(MkSet(eParameter));
        Prim := Iter.FirstSchObject;
        while Prim <> nil do
        begin
            List1.Add(Prim.Name + '=' + Prim.Text);
            Prim := Iter.NextSchObject;
        end;
        Obj2.SchIterator_Destroy(Iter);
        SandboxLog('Child params: ' + List1.CommaText);
        List1.Free;

        ResultText := '{"done": true}';
        // === END EXPERIMENT ===
    except
        SandboxLog('EXCEPTION escaped the script body');
        ResultText := '{"error": "exception escaped script - see log for last step"}';
    end;

    SandboxLog('sandbox end');

    OutLines := TStringList.Create;
    try
        OutLines.Text := ResultText;
        OutLines.SaveToFile(OutPath);
    finally
        OutLines.Free;
    end;
end;
