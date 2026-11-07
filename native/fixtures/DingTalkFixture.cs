// Synthetic integration fixture. This is NOT DingTalk and contains no real data.
// A fully transparent, non-taskbar window tests out-of-process accessibility
// without changing focus or showing an interactive window on the user's desktop.
using System;
using System.Drawing;
using System.Windows.Forms;

internal sealed class Fixture : Form
{
    private readonly Label evidence;
    private int tick;
    protected override bool ShowWithoutActivation { get { return true; } }
    private Fixture()
    {
        Text = "Synthetic UIA fixture";
        ShowInTaskbar = false;
        FormBorderStyle = FormBorderStyle.FixedToolWindow;
        StartPosition = FormStartPosition.Manual;
        Location = new Point(30, 30);
        Size = new Size(420, 240);
        Opacity = 0;
        evidence = new Label { Text = "fixture-safe-evidence-0", AccessibleName = "fixture-safe-evidence-0", AutoSize = true, Location = new Point(10, 10) };
        Controls.Add(evidence);
        Controls.Add(new TextBox { Text = "fixture-secret-password", UseSystemPasswordChar = true, Location = new Point(10, 50) });
        Controls.Add(new TextBox { Text = "fixture-secret-draft", AccessibleName = "fixture-secret-search", Location = new Point(10, 90) });
        Panel aggregate = new Panel { AccessibleRole = AccessibleRole.ListItem, AccessibleName = "fixture-secret-aggregated-parent", Location = new Point(200, 50), Size = new Size(180, 80) };
        aggregate.Controls.Add(new TextBox { Text = "fixture-secret-descendant", UseSystemPasswordChar = true, Size = new Size(150, 20) });
        Controls.Add(aggregate);
        Timer timer = new Timer { Interval = 400 };
        timer.Tick += delegate {
            tick++;
            evidence.Text = "fixture-safe-evidence-" + tick;
            evidence.AccessibleName = evidence.Text;
            if (tick == 4) Controls.Add(new Label { Text = "fixture-added-evidence", AutoSize = true, Location = new Point(10, 130) });
            if (tick >= 40) Close();
        };
        timer.Start();
    }
    [STAThread] private static void Main() { Application.Run(new Fixture()); }
}
