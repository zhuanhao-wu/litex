#
# This file is part of LiteX.
#
# Copyright (c) 2019-2024 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2019 Antti Lukats <antti.lukats@gmail.com>
# Copyright (c) 2021 Franck Jullien <franck.jullien@collshade.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.fhdl.specials import Tristate
from migen.genlib.cdc    import MultiReg

from litex.gen import *
from litex.gen.genlib.misc import WaitTimer

from litex.soc.interconnect.csr import *
from litex.soc.interconnect import stream

from litex.build.io import DifferentialOutput

from litex.soc.interconnect import wishbone

# HyperRAMPHY --------------------------------------------------------------------------------------

class HyperRAMPHY(LiteXModule):
    def __init__(self, pads, data_width, clk_domain="sys"):
        self.rst     = Signal()              # i.
        self.cs      = Signal()              # i.
        self.dq_o    = Signal(data_width)    # i.
        self.dq_oe   = Signal()              # i.
        self.dq_i    = Signal(data_width)    # o.
        self.rwds_o  = Signal(data_width//8) # i.
        self.rwds_oe = Signal()              # i.
        self.rwds_i  = Signal(data_width//8) # o.
        self.shift   = Signal()              # o.

        # # #

        # Parameters.
        # ----------
        self.clk_domain = clk_domain
        _sync = getattr(self.sync, clk_domain)

        # Rst.
        # ----
        if hasattr(pads, "rst_n"):
            _sync += pads.rst_n.eq(~self.rst)

        # CS_n.
        # -----
        pads.cs_n.reset = 2**len(pads.cs_n) - 1
        _sync += pads.cs_n[0].eq(~self.cs) # Only supporting one Chip.

        # Clk Gen.
        # --------
        clk       = Signal()
        clk_d     = Signal()
        clk_phase = Signal(2)
        _sync += [
            clk_phase.eq(0b00),
            If(self.cs,
                clk_phase.eq(clk_phase + 1)
            ),
            Case(clk_phase, {
                0b00 : clk.eq(0),       #   0°.
                0b01 : clk.eq(self.cs), #  90°.
                0b10 : clk.eq(self.cs), # 180°.
                0b11 : clk.eq(0),       # 270°.
            })
        ]
        self.specials += MultiReg(clk, clk_d, clk_domain, n={"sys": 0, "sys2x": 1}[clk_domain])
        self.comb += self.shift.eq(clk_phase[0] == 0 | (clk_domain == "sys2x"))

        # Clk Out.
        # --------
        # Single Ended Clk.
        if hasattr(pads, "clk"):
            self.comb += pads.clk.eq(clk_d)
        # Differential Clk.
        elif hasattr(pads, "clk_p"):
            self.specials += DifferentialOutput(clk_d, pads.clk_p, pads.clk_n)
        else:
            raise ValueError

        # DQ.
        # ---
        dq = pads.dq
        if not hasattr(dq, "oe"):
            dq = self.add_tristate(dq)
        self.comb += dq.o.eq(  self.dq_o)
        self.comb += dq.oe.eq(self.dq_oe)
        _sync += self.dq_i.eq(dq.i) # FIXME: Use phase-shifted Clk?

        # RWDS.
        # -----
        rwds = pads.rwds
        if not hasattr(rwds, "oe"):
            rwds = self.add_tristate(pads.rwds)
        self.comb += rwds.o.eq(  self.rwds_o)
        self.comb += rwds.oe.eq( self.rwds_oe)
        _sync += self.rwds_i.eq(rwds.i) # FIXME: Use phase-shifted Clk?

    def add_tristate(self, pad):
        class TristatePads:
            def __init__(self, width=1):
                self.o  = Signal(width)
                self.oe = Signal()
                self.i  = Signal(width)
        t = TristatePads(width=len(pad))
        self.specials += Tristate(pad,
            o   = t.o,
            oe  = t.oe,
            i   = t.i,
        )
        return t

# HyperRAM -----------------------------------------------------------------------------------------

class HyperRAM(LiteXModule):
    tCSM = 4e-6
    """HyperRAM

    Provides a very simple/minimal HyperRAM core with a Wishbone Interface that can work with all
    FPGA/HyperRam chips:
    - Vendor agnostic.
    - Fixed/Variable latency.
    - Latency/Registers (re-)configuration.

    Parameters:
        pads (Record)                  : Interface to the HyperRAM connection pads.
        latency (int, optional)        : Initial latency setting, defaults to 6.
        latency_mode (str, optional)   : Specifies the latency mode ('fixed' or 'variable'), defaults to 'variable'.
        sys_clk_freq (float, optional) : System clock frequency in Hz.
        with_csr (bool, optional)      : Enables CSR interface for Latency/Registers configuration, defaults to True.

    Attributes:
        pads (Record)            : Platform pads of HyperRAM.
        bus (wishbone.Interface) : Wishbone Interface.
"""
    def __init__(self, pads, latency=6, latency_mode="variable", sys_clk_freq=10e6, clk_ratio="4:1", with_csr=True):
        self.pads = pads
        self.bus  = bus = wishbone.Interface(data_width=32, address_width=32, addressing="word")

        # # #

        # Parameters.
        # -----------
        data_width = len(getattr(pads.dq, "o", pads.dq))
        assert data_width   in [8, 16]
        assert latency_mode in ["fixed", "variable"]
        assert clk_ratio    in [
            "4:1", # HyperRAM Clk = Sys Clk/4.
            "2:1", # HyperRAM Clk = Sys Clk/2.
        ]

        # PHY.
        # ----
        self.phy = phy = HyperRAMPHY(
            pads       = pads,
            data_width = data_width,
            clk_domain = {"4:1": "sys", "2:1": "sys2x"}[clk_ratio],
        )

        # Config/Reg Interface.
        # ---------------------
        self.conf_rst          = Signal()
        self.conf_latency      = Signal(8, reset=latency)
        self.stat_latency_mode = Signal(reset={"fixed": 0, "variable": 1}[latency_mode])
        self.reg_wr            = Signal()
        self.reg_rd            = Signal()
        self.reg_addr          = Signal(2)
        self.reg_done          = Signal()
        self.reg_wr_data       = Signal(16)
        self.reg_rd_data       = Signal(16)
        if with_csr:
            self.add_csr(default_latency=latency)

        # Internal Signals.
        # -----------------
        cmd_addr            = Signal(48)
        cmd_addr_oe         = Signal()
        shift_reg_load      = Signal()
        shift_reg_load_data = Signal(48)
        shift_reg_data      = Signal(48)
        shift_reg_data_next = Signal(48)

        # Rst --------------------------------------------------------------------------------------
        self.comb += phy.rst.eq(self.conf_rst)

        # Burst Timer ------------------------------------------------------------------------------
        self.burst_timer = burst_timer = WaitTimer(sys_clk_freq * self.tCSM)

        # Shift Register ---------------------------------------------------------------------------

        # Shift & Input/Output Data.
        self.comb += [
            # During Command/Address Phase, only shift 8-bit per cycle.
            If(cmd_addr_oe,
                phy.dq_o.eq(shift_reg_data[-8:]),                    # -> Output.
                shift_reg_data_next[:8].eq(0),                       # <- Input (No Data).
                shift_reg_data_next[8:].eq(shift_reg_data),          # Shift.
            ),
            # During Data Phase, shift data_width-bit per cycle.
            If(~cmd_addr_oe,
                phy.dq_o.eq(shift_reg_data[-data_width:]),           # -> Output.
                shift_reg_data_next[:data_width].eq(phy.dq_i),       # <- Input.
                shift_reg_data_next[data_width:].eq(shift_reg_data), # Shift.
            )
        ]
        self.sync += If(phy.shift, shift_reg_data.eq(shift_reg_data_next))

        # Load.
        self.sync += If(shift_reg_load, shift_reg_data.eq(shift_reg_load_data))

        # Register Access/Buffer -------------------------------------------------------------------
        reg_wr_req = Signal()
        reg_rd_req = Signal()
        self.reg_buf = reg_buf = stream.SyncFIFO(
            layout = [("write", 1), ("read", 1), ("addr", 4), ("data", 16)],
            depth  = 4,
        )
        reg_ep = reg_buf.source
        self.comb += [
            reg_buf.sink.valid.eq(self.reg_wr | self.reg_rd),
            reg_buf.sink.write.eq(self.reg_wr),
            reg_buf.sink.read.eq(self.reg_rd),
            reg_buf.sink.addr.eq(self.reg_addr),
            reg_buf.sink.data.eq(self.reg_wr_data),
            reg_wr_req.eq(reg_ep.valid & reg_ep.write),
            reg_rd_req.eq( reg_ep.valid & reg_ep.read),
        ]
        self.sync += If(reg_buf.sink.valid,
            self.reg_done.eq(0),
        )

        # Command generation -----------------------------------------------------------------------
        ashift = {8:1, 16:0}[data_width]
        self.comb += [
            # Register Command Generation.
            If(reg_wr_req | reg_rd_req,
                cmd_addr[47].eq(reg_ep.read), # R/W#
                cmd_addr[46].eq(1),           # Register Space.
                cmd_addr[45].eq(1),           # Burst Type (Linear)
                Case(reg_ep.addr, {
                    0 : cmd_addr[0:40].eq(0x00_00_00_00_00), # Identification Register 0 (Read Only).
                    1 : cmd_addr[0:40].eq(0x00_00_00_00_01), # Identification Register 1 (Read Only).
                    2 : cmd_addr[0:40].eq(0x00_01_00_00_00), # Configuration Register 0.
                    3 : cmd_addr[0:40].eq(0x00_01_00_00_01), # Configuration Register 1.
                }),
            # Wishbone Command Generation.
            ).Else(
                cmd_addr[47].eq(~bus.we),                # R/W#
                cmd_addr[46].eq(0),                      # Memory Space.
                cmd_addr[45].eq(1),                      # Burst Type (Linear)
                cmd_addr[16:45].eq(bus.adr[3-ashift:]),  # Row & Upper Column Address
                cmd_addr[ashift:3].eq(bus.adr),          # Lower Column Address
            )
        ]

        # Bus Latch/Output -------------------------------------------------------------------------

        # Latch.
        bus_adr   = Signal(32)
        bus_we    = Signal()
        bus_sel   = Signal(4)
        bus_latch = Signal()
        self.sync += If(bus_latch,
            bus_we.eq(bus.we),
            bus_sel.eq(bus.sel),
            bus_adr.eq(bus.adr)
        )
        self.comb += If(bus_latch & bus.we,
            shift_reg_load.eq(1),
            shift_reg_load_data.eq(Cat(Signal(16), bus.dat_w)),
        )
        # Output.
        self.comb += bus.dat_r.eq(shift_reg_data_next)

        # FSM (Sequencer) --------------------------------------------------------------------------
        cycles  = Signal(8)
        first   = Signal()
        refresh = Signal()
        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            NextValue(first, 1),
            If((bus.cyc & bus.stb) | reg_wr_req | reg_rd_req,
                shift_reg_load.eq(1),
                shift_reg_load_data.eq(cmd_addr),
                NextState("SEND-COMMAND-ADDRESS")
            )
        )
        fsm.act("SEND-COMMAND-ADDRESS",
            # Send Command on DQ.
            cmd_addr_oe.eq(1),
            phy.dq_oe.eq(1),
            # Wait for 6*2 cycles.
            If(cycles == (6*2 - 1),
                If(reg_wr_req,
                    shift_reg_load.eq(1),
                    shift_reg_load_data.eq(Cat(Signal(40), self.reg_wr_data[8:])),
                    NextState("REG-WRITE-0")
                ).Else(
                    # Sample RWDS to know if 1X/2X Latency should be used (Refresh).
                    NextValue(refresh, phy.rwds_i | (latency_mode in ["fixed"])),
                    NextState("WAIT-LATENCY")
                )
            )
        )
        fsm.act("REG-WRITE-0",
            # Send Reg on DQ.
            cmd_addr_oe.eq(1),
            phy.dq_oe.eq(1),
            # Wait for 2 cycles.
            If(cycles == (2 - 1),
                shift_reg_load.eq(1),
                shift_reg_load_data.eq(Cat(Signal(40), self.reg_wr_data[:8])),
                NextState("REG-WRITE-1")
            )
        )
        fsm.act("REG-WRITE-1",
            # Send Reg on DQ.
            cmd_addr_oe.eq(1),
            phy.dq_oe.eq(1),
            # Wait for 2 cycles.
            If(cycles == (2 - 1),
                reg_ep.ready.eq(1),
                NextValue(self.reg_done, 1),
                NextState("IDLE")
            )
        )
        fsm.act("WAIT-LATENCY",
            # Wait for 1X or 2X Latency cycles... (-4 since count start in the middle of the command).
            If(((cycles == 2*(self.conf_latency * 4) - 4 - 1) &  refresh) | # 2X Latency (No DRAM refresh required).
               ((cycles == 1*(self.conf_latency * 4) - 4 - 1) & ~refresh) , # 1X Latency (   DRAM refresh required).
                # Latch Bus.
                bus_latch.eq(1),
                # Early Write Ack (to allow bursting).
                If(~reg_rd_req,
                    bus.ack.eq(bus.we),
                ),
                NextState("READ-WRITE-DATA0")
            )
        )
        states = {8:4, 16:2}[data_width]
        for n in range(states):
            fsm.act(f"READ-WRITE-DATA{n}",
                # Enable Burst Timer.
                burst_timer.wait.eq(1),
                cmd_addr_oe.eq(reg_rd_req),
                # Send Data on DQ/RWDS (for write).
                If(bus_we,
                    phy.dq_oe.eq(1),
                    phy.rwds_oe.eq(1),
                    *[phy.rwds_o[data_width//8-1-i].eq(~bus_sel[4-1-n*data_width//8-i]) for i in range(data_width//8)],
                ),
                # Wait for 2 cycles.
                If(cycles == (2 - 1),
                    # Set next default state (with rollover for bursts).
                    NextState(f"READ-WRITE-DATA{(n + 1)%states}"),
                    # On last state, see if we can continue the burst or if we should end it.
                    If(n == (states - 1),
                        NextValue(first, 0),
                        # Continue burst when a consecutive access is ready.
                        If(~reg_rd_req & bus.stb & bus.cyc & (bus.we == bus_we) & (bus.adr == (bus_adr + 1)) & (~burst_timer.done),
                            # Latch Bus.
                            bus_latch.eq(1),
                            # Early Write Ack (to allow bursting).
                            bus.ack.eq(bus.we)
                        # Else end the burst.
                        ).Elif(bus_we | (~first) | burst_timer.done,
                            NextState("IDLE")
                        )
                    ),
                    # Read Ack (when dat_r ready).
                    If((n == 0) & ~first,
                        If(reg_rd_req,
                            reg_ep.ready.eq(1),
                            NextValue(self.reg_done, 1),
                            NextValue(self.reg_rd_data, bus.dat_r),
                            NextState("IDLE"),
                        ).Else(
                            bus.ack.eq(~bus_we),
                        )
                    )
                )
            )

        # CS --------------------------------------------------------------------------------------
        self.comb += If(~fsm.ongoing("IDLE"),        phy.cs.eq(1)) # CS when not in IDLE state.
        self.comb += If(fsm.before_leaving("IDLE"),  phy.cs.eq(1)) # Early Set.
        self.comb += If(fsm.before_entering("IDLE"), phy.cs.eq(0)) # Early Clr.

        # FSM Cycles -------------------------------------------------------------------------------
        fsm.finalize()
        cycles_rst = {
            "4:1" : 0,
            "2:1" : 1,
        }[clk_ratio]
        cycles_inc = {
            "4:1" : 1,
            "2:1" : 2,
        }[clk_ratio]
        self.sync += cycles.eq(cycles + cycles_inc)
        self.sync += If(fsm.next_state != fsm.state, cycles.eq(cycles_rst))

    def add_csr(self, default_latency=6):
        # Config/Status Interface.
        # ------------------------
        self.config = CSRStorage(fields=[
            CSRField("rst",     offset=0, size=1, pulse=True, description="HyperRAM Rst."),
            CSRField("latency", offset=8, size=8,             description="HyperRAM Latency (X1).", reset=default_latency),
        ])
        self.comb += [
            self.conf_rst.eq(    self.config.fields.rst),
            self.conf_latency.eq(self.config.fields.latency),
        ]
        self.status = CSRStatus(fields=[
            CSRField("latency_mode", offset=0, size=1, values=[
                ("``0b0``", "Fixed Latency."),
                ("``0b1``", "Variable Latency."),
            ]),
            CSRField("clk_ratio", offset=1, size=4, values=[
                ("``4``", "HyperRAM Clk = Sys Clk/4."),
                ("``2``", "HyperRAM Clk = Sys Clk/2."),
            ]),
        ])
        self.comb += [
            self.status.fields.latency_mode.eq(self.stat_latency_mode),
            self.status.fields.clk_ratio.eq({
                "sys"  : 4,
                "sys2x": 2,
            }[self.phy.clk_domain]),
        ]

        # Reg Interface.
        # --------------
        self.reg_control = CSRStorage(fields=[
            CSRField("write", offset=0, size=1, pulse=True, description="Issue Register Write."),
            CSRField("read",  offset=1, size=1, pulse=True, description="Issue Register Read."),
            CSRField("addr",  offset=8, size=4, values=[
                ("``0b00``", "Identification Register 0 (Read Only)."),
                ("``0b01``", "Identification Register 1 (Read Only)."),
                ("``0b10``", "Configuration Register 0."),
                ("``0b11``", "Configuration Register 1."),
            ]),
        ])
        self.reg_status = CSRStatus(fields=[
            CSRField("done", offset=0, size=1, description="Register Access Done."),
        ])
        self.reg_wdata = CSRStorage(16, description="Register Write Data.")
        self.reg_rdata = CSRStatus( 16, description="Register Read Data.")

        self.comb += [
            # Control.
            self.reg_wr.eq(self.reg_control.fields.write),
            self.reg_rd.eq( self.reg_control.fields.read),
            self.reg_addr.eq( self.reg_control.fields.addr),

            # Status.
            self.reg_status.fields.done.eq(self.reg_done),

            # Data.
            self.reg_wr_data.eq(self.reg_wdata.storage),
            self.reg_rdata.status.eq(self.reg_rd_data),
        ]
