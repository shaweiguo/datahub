package datahub.protobuf.visitors.dataset;

import com.linkedin.common.GlossaryTermAssociation;
import com.linkedin.common.urn.GlossaryTermUrn;
import org.junit.Test;

import java.io.IOException;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

import static datahub.protobuf.TestFixtures.getTestProtobufGraph;
import static datahub.protobuf.TestFixtures.getVisitContextBuilder;
import static org.junit.jupiter.api.Assertions.assertEquals;


public class ProtobufExtensionTermAssocVisitorTest {

    @Test
    public void extendedMessageTest() throws IOException {
        ProtobufExtensionTermAssocVisitor test = new ProtobufExtensionTermAssocVisitor();
        assertEquals(Set.of(
                        new GlossaryTermAssociation().setUrn(new GlossaryTermUrn("Classification.HighlyConfidential"))
                ),
                getTestProtobufGraph("extended_protobuf", "messageA")
                        .accept(getVisitContextBuilder("extended_protobuf.Person"),
                                List.of(test)).collect(Collectors.toSet()));
    }

    @Test
    public void extendedFieldTest() throws IOException {
        ProtobufExtensionTermAssocVisitor test = new ProtobufExtensionTermAssocVisitor();
        assertEquals(
                Set.of(),
                getTestProtobufGraph("extended_protobuf", "messageB").
                        accept(getVisitContextBuilder("extended_protobuf.Person"), List.of(test)).collect(Collectors.toSet()));
    }
}
