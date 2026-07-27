# demo.drawio

## page demo  vertices=18 edges=16
## V 图元 | T 文本 | J 结点 | W 连线
## 端点  id:x,y=接在图元上 | @x,y=悬空或折点 | id:?=无约束点 | ~=近似(未做perimeter投影)

## styles
S1   x1   electrical.transistors.pmos flipH=1
S2   x1   electrical.transistors.pmos
S3   x2   electrical.transistors.nmos
S4   x1   electrical.transistors.nmos flipH=1
S5   x2   electrical.signal_sources.vss2 flipV=1 fontSize=18
S6   x1   electrical.capacitors.capacitor_2 direction=south
S7   x6   fillColor=none strokeColor=none text
S8   x1   fillColor=#dae8fc strokeColor=#6c8ebf
S9   x1   fillColor=#d5e8d4 rounded=1 strokeColor=#82b366
E1   x1   endArrow=none
E2   x14  endArrow=none endFill=0
E3   x1   endArrow=classic strokeWidth=2

## cells
V 3    S1  200,180,50,40
V 4    S2  400,180,50,40
V 5    S3  170,300,50,40 {L=0.5u} {W=10u} {m=4} {note=input pair}
V 6    S4  430,300,50,40
V 7    S3  300,400,50,40
V 8    S5  343,470,14,21  "Vss"
V 9    S5  543,420,14,21  "Vss"
V 10   S6  537,330,25,50
J 20   325,200
J 21   450,240
T 30   S7  100,100,50,30  "Vdd"
T 31   S7  90,305,40,30  "inp"
T 32   S7  520,290,40,30  "inn"
T 33   S7  145,340,30,30  "x4"
T 34   S7  570,345,80,30  "*CL=2pF*"
V 40   S8  150,560,140,60  "BIAS GEN"
V 41   S9  400,560,140,60  "*OTA CORE*"
T 42   S7  150,650,240,30  "色标: 蓝=VDD_1V8, 绿=VDD_3V3"
W 2    E1  @120,120 > @560,120
W 50   E2  3:200,180 > @200,120
W 51   E2  4:450,180 > @450,120
W 52   E2  3:200,220 > 5:220,300
W 53   E2  4:450,220 > 6:430,300
W 54   E2  3:250,200 > 4:400,200
W 55   E2  3:200,220 > 3:250,200
W 56   E2  5:220,340 > @220,370 > @350,370 > 7:350,400
W 57   E2  6:430,340 > 7:350,400
W 58   E2  7:350,440 > 8:350,470
W 59   E2  7:300,420 > @240,420  |"vbias"@-0.6
W 60   E2  21:450,240 > 10:549.5,330
W 61   E2  10:549.5,380 > 9:550,420
W 62   E2  5:170,320 > @130,320
W 63   E2  6:480,320 > @520,320
W 64   E3  40:290,590 > 41:400,590  |"vbias"@0.1
