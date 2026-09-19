// ina219_reader: on `trigger` (every POWER_PERIOD_MS, from node_ctrl) read
// the INA219 bus-voltage register (pointer 0x02) and shunt-voltage register
// (pointer 0x01) raw, exactly as protocol.md §4.11 wants them: no
// calibration register, no arithmetic on the node.
//   START, W addr|0, W ptr, START, W addr|1, R msb (ACK), R lsb (NACK), STOP
// twice. `ok` = the bus read high at every START and every address/pointer
// write was acknowledged, so a missing breakout (NACKs) or a dead/absent bus
// (SDA stuck low) gives ok=0 with the sequence still completing, and POWER
// frames keep flowing with valid=0, which is what the dashboard needs to
// show absence.

module ina219_reader #(
    parameter [6:0] I2C_ADDR = 7'h40,
    parameter CLKS_PER_QUARTER = 250
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        trigger,
    output reg         sample_valid,   // one-cycle pulse: bus_raw / shunt_raw / ok updated
    output reg  [15:0] bus_raw,
    output reg  [15:0] shunt_raw,
    output reg         ok,
    output wire        scl_oe,
    output wire        sda_oe,
    input  wire        sda_i
);
    reg        cmd_valid;
    reg  [1:0] cmd;
    reg  [7:0] wdata;
    reg        read_nack;
    wire       busy, done, ack;
    wire [7:0] rdata;

    i2c_master #(.CLKS_PER_QUARTER(CLKS_PER_QUARTER)) m (
        .clk(clk), .rst_n(rst_n), .cmd_valid(cmd_valid), .cmd(cmd), .wdata(wdata), .read_nack(read_nack),
        .busy(busy), .done(done), .rdata(rdata), .ack(ack), .scl_oe(scl_oe), .sda_oe(sda_oe), .sda_i(sda_i)
    );

    // one flat script of 16 commands: step 0..7 reads pointer 0x02, 8..15 pointer 0x01
    reg [4:0] step;                 // 16 = idle
    reg       running;
    reg       acks_ok;
    reg [15:0] bus_acc, shunt_acc;
    wire [2:0] sub = step[2:0];
    wire       second = step[3];
    wire [7:0] ptr = second ? 8'h01 : 8'h02;

    always @(posedge clk) begin
        if (!rst_n) begin
            step <= 5'd0; running <= 1'b0; cmd_valid <= 1'b0; cmd <= 2'd0; wdata <= 8'd0; read_nack <= 1'b0;
            acks_ok <= 1'b0; bus_acc <= 16'd0; shunt_acc <= 16'd0;
            sample_valid <= 1'b0; bus_raw <= 16'd0; shunt_raw <= 16'd0; ok <= 1'b0;
        end else begin
            sample_valid <= 1'b0;
            cmd_valid <= 1'b0;
            if (!running) begin
                if (trigger) begin
                    running <= 1'b1;
                    step <= 5'd0;
                    acks_ok <= 1'b1;
                end
            end else if (!busy && !cmd_valid && !done) begin
                // (!done: `step` advances in the done cycle, so issuing there would repeat a command)
                if (step == 5'd16) begin
                    running <= 1'b0;
                    bus_raw <= bus_acc;
                    shunt_raw <= shunt_acc;
                    ok <= acks_ok;
                    sample_valid <= 1'b1;
                end else begin
                    cmd_valid <= 1'b1;
                    read_nack <= 1'b0;
                    case (sub)
                        3'd0: cmd <= 2'd0;                                       // START
                        3'd1: begin cmd <= 2'd1; wdata <= {I2C_ADDR, 1'b0}; end  // address, write
                        3'd2: begin cmd <= 2'd1; wdata <= ptr; end               // register pointer
                        3'd3: cmd <= 2'd0;                                       // repeated START
                        3'd4: begin cmd <= 2'd1; wdata <= {I2C_ADDR, 1'b1}; end  // address, read
                        3'd5: cmd <= 2'd2;                                       // MSB, ACK
                        3'd6: begin cmd <= 2'd2; read_nack <= 1'b1; end          // LSB, NACK
                        3'd7: cmd <= 2'd3;                                       // STOP
                    endcase
                end
            end
            // harvest results as each command completes
            if (running && done) begin
                if ((cmd == 2'd0 || cmd == 2'd1) && !ack) acks_ok <= 1'b0;   // START: bus high; WRITE: ACKed
                if (cmd == 2'd2 && sub == 3'd5) begin
                    if (second) shunt_acc[15:8] <= rdata; else bus_acc[15:8] <= rdata;
                end
                if (cmd == 2'd2 && sub == 3'd6) begin
                    if (second) shunt_acc[7:0] <= rdata; else bus_acc[7:0] <= rdata;
                end
                step <= step + 1'b1;
            end
        end
    end
endmodule
